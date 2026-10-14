//! The Waveshare 10.3" e-Paper HAT (B): an IT8951 controller driving an
//! ED103TC2 panel, 1872x1404 in sixteen greys, on a Pi's SPI.
//!
//! The kernel's chip select is moved off GPIO8 (`dtoverlay=spi0-1cs,
//! cs0_pin=7`) and the HAT's, GPIO8, is driven here, held low for a whole
//! command; RST is GPIO17, and HRDY (GPIO24) must be high before every
//! word. Every SPI exchange opens with a preamble: 0x6000 a command, 0x0000
//! data, 0x1000 a read. Commands go at 2 MHz, pixels at 12 MHz.
//!
//! VCOM is set every start, since the board forgets it. The panel scans
//! mirrored as seen from the front, so every row is loaded right to left.

use std::io;
use std::thread::sleep;
use std::time::{Duration, Instant};

use rppal::gpio::{Gpio, InputPin, OutputPin};
use rppal::spi::{Bus, Mode as SpiMode, SlaveSelect, Spi};

use super::Panel;
use crate::geom::Rect;
use crate::policy::Mode;
use crate::tune::{SPI_CHUNK, SPI_FAST, SPI_SLOW, VCOM_MV};

const RST: u8 = 17;
const HRDY: u8 = 24;
const CS: u8 = 8;

const PRE_CMD: u16 = 0x6000;
const PRE_WRITE: u16 = 0x0000;
const PRE_READ: u16 = 0x1000;

const SYS_RUN: u16 = 0x0001;
const SLEEP: u16 = 0x0003;
const REG_RD: u16 = 0x0010;
const REG_WR: u16 = 0x0011;
const LD_IMG_AREA: u16 = 0x0021;
const LD_IMG_END: u16 = 0x0022;
const DPY_AREA: u16 = 0x0034;
const VCOM: u16 = 0x0039;
const GET_DEV_INFO: u16 = 0x0302;

const I80CPCR: u16 = 0x0004;
const LISAR: u16 = 0x0208;
const LUTAFSR: u16 = 0x1224;

/// How pixels are packed on the way to the controller.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Depth {
    /// Two pixels a word, the first in the low byte.
    Eight,
    /// Four pixels a word, the first in the low nibble: half the bytes.
    Four,
}

fn err<E: std::fmt::Display>(what: &str) -> impl FnOnce(E) -> io::Error + '_ {
    move |e| io::Error::other(format!("{what}: {e}"))
}

pub struct It8951 {
    spi: Spi,
    cs: OutputPin,
    rst: OutputPin,
    hrdy: InputPin,
    speed: u32,
    pub w: u32,
    pub h: u32,
    addr: u32,
    pub firmware: String,
    pub lut: String,
    pub depth: Depth,
    /// The panel is mirrored as it scans.
    pub mirrored: bool,
    asleep: bool,
    vcom_mv: u16,
}

impl It8951 {
    /// Reset the controller, read what it is, and set VCOM (`vcom_mv`
    /// millivolts below zero; None for this panel's own).
    pub fn open(vcom_mv: Option<u16>, depth: Depth) -> io::Result<It8951> {
        let gpio = Gpio::new().map_err(err("gpio"))?;
        let cs = gpio.get(CS).map_err(err("gpio 8"))?.into_output_high();
        let rst = gpio.get(RST).map_err(err("gpio 17"))?.into_output_high();
        let hrdy = gpio.get(HRDY).map_err(err("gpio 24"))?.into_input();
        let spi = Spi::new(Bus::Spi0, SlaveSelect::Ss0, SPI_SLOW, SpiMode::Mode0).map_err(err("spi"))?;
        let mut p = It8951 {
            spi,
            cs,
            rst,
            hrdy,
            speed: SPI_SLOW,
            w: 0,
            h: 0,
            addr: 0,
            firmware: String::new(),
            lut: String::new(),
            depth,
            mirrored: true,
            asleep: false,
            vcom_mv: vcom_mv.unwrap_or(VCOM_MV),
        };
        p.start()?;
        Ok(p)
    }

    /// Reset the controller and bring it up: what it is, VCOM, packed
    /// writes. As it is opened, and again whenever it stops answering.
    fn start(&mut self) -> io::Result<()> {
        let p = self;
        p.asleep = false;
        p.rst.set_low();
        sleep(Duration::from_millis(100));
        p.rst.set_high();
        sleep(Duration::from_millis(500));
        p.command(SYS_RUN, &[])?;
        p.command(GET_DEV_INFO, &[])?;
        let info = p.read(20)?;
        p.w = info[0] as u32;
        p.h = info[1] as u32;
        p.addr = (info[3] as u32) << 16 | info[2] as u32;
        let text = |ws: &[u16]| -> String {
            ws.iter().flat_map(|w| [(w & 0xff) as u8, (w >> 8) as u8]).take_while(|&b| b != 0).map(|b| b as char).collect()
        };
        p.firmware = text(&info[4..12]);
        p.lut = text(&info[12..20]);
        if p.w == 0 || p.h == 0 || p.w > 4096 || p.h > 4096 {
            return Err(io::Error::other(format!("the controller says it is {}x{}: is the HAT on SPI?", p.w, p.h)));
        }
        let vcom = p.vcom_mv;
        p.command(VCOM, &[1, vcom])?;
        p.reg_write(I80CPCR, 1)?;
        Ok(())
    }

    /// VCOM as the controller has it, in millivolts below zero.
    pub fn vcom(&mut self) -> io::Result<u16> {
        self.command(VCOM, &[0])?;
        Ok(self.read(1)?[0])
    }

    fn ready(&self) -> io::Result<()> {
        let end = Instant::now() + Duration::from_secs(10);
        while self.hrdy.is_low() {
            if Instant::now() > end {
                return Err(io::Error::other("HRDY never rose"));
            }
            std::hint::spin_loop();
        }
        Ok(())
    }

    fn speed(&mut self, hz: u32) -> io::Result<()> {
        if self.speed != hz {
            self.spi.set_clock_speed(hz).map_err(err("spi speed"))?;
            self.speed = hz;
        }
        Ok(())
    }

    fn word(&mut self, w: u16) -> io::Result<()> {
        self.spi.write(&w.to_be_bytes()).map_err(err("spi write"))?;
        self.ready()
    }

    /// One exchange: a preamble, words, then pixels or a read.
    fn exchange(&mut self, pre: u16, words: &[u16], bulk: Option<&[u8]>, read: usize) -> io::Result<Vec<u16>> {
        self.speed(SPI_SLOW)?;
        self.ready()?;
        self.cs.set_low();
        let r = (|| {
            self.word(pre)?;
            for &w in words {
                self.word(w)?;
            }
            if let Some(b) = bulk {
                self.speed(SPI_FAST)?;
                for c in b.chunks(SPI_CHUNK) {
                    self.spi.write(c).map_err(err("spi bulk"))?;
                }
                self.speed(SPI_SLOW)?;
            }
            let mut out = Vec::with_capacity(read);
            if read > 0 {
                let mut buf = [0u8; 2];
                self.spi.transfer(&mut buf, &[0, 0]).map_err(err("spi read"))?;
                self.ready()?;
                for _ in 0..read {
                    self.spi.transfer(&mut buf, &[0, 0]).map_err(err("spi read"))?;
                    out.push(u16::from_be_bytes(buf));
                    self.ready()?;
                }
            }
            Ok(out)
        })();
        self.cs.set_high();
        r
    }

    fn command(&mut self, c: u16, args: &[u16]) -> io::Result<()> {
        self.exchange(PRE_CMD, &[c], None, 0)?;
        for &a in args {
            self.exchange(PRE_WRITE, &[a], None, 0)?;
        }
        Ok(())
    }

    fn read(&mut self, n: usize) -> io::Result<Vec<u16>> {
        self.exchange(PRE_READ, &[], None, n)
    }

    fn reg_write(&mut self, a: u16, v: u16) -> io::Result<()> {
        self.command(REG_WR, &[a, v])
    }

    fn reg_read(&mut self, a: u16) -> io::Result<u16> {
        self.command(REG_RD, &[a])?;
        Ok(self.read(1)?[0])
    }

    fn wake(&mut self) -> io::Result<()> {
        if self.asleep {
            self.command(SYS_RUN, &[])?;
            self.asleep = false;
        }
        Ok(())
    }

    /// Wait until the display engine has finished every refresh.
    pub fn wait(&mut self) -> io::Result<()> {
        let end = Instant::now() + Duration::from_secs(30);
        while self.reg_read(LUTAFSR)? != 0 {
            if Instant::now() > end {
                return Err(io::Error::other("the display never finished"));
            }
            sleep(Duration::from_millis(10));
        }
        Ok(())
    }

    /// The waveform mode number this panel's firmware gives `mode`.
    fn mode_number(&self, mode: Mode) -> u16 {
        match mode {
            Mode::Init => 0,
            Mode::Du => 1,
            Mode::Gc16 => 2,
        }
    }

    /// Load greys for a panel rectangle seen from the front.
    pub fn load(&mut self, r: &Rect, grey: &[u8]) -> io::Result<Rect> {
        self.wake()?;
        self.wait()?;
        // As the panel scans: mirrored rows, so the box moves across.
        let s = if self.mirrored { Rect::new(self.w - r.x - r.w, r.y, r.w, r.h) } else { *r };
        let (bpp, per) = match self.depth {
            Depth::Eight => (3u16, 2usize),
            Depth::Four => (2u16, 4usize),
        };
        assert_eq!(r.w as usize % per, 0, "a load is aligned to its packing");
        let mut data = Vec::with_capacity(r.area() as usize * 2 / per);
        for y in 0..r.h as usize {
            let row = &grey[y * r.w as usize..(y + 1) * r.w as usize];
            let px = |i: usize| if self.mirrored { row[row.len() - 1 - i] } else { row[i] };
            for i in (0..row.len()).step_by(per) {
                let w: u16 = match self.depth {
                    Depth::Eight => px(i) as u16 | (px(i + 1) as u16) << 8,
                    Depth::Four => {
                        (px(i) >> 4) as u16 | ((px(i + 1) >> 4) as u16) << 4 | ((px(i + 2) >> 4) as u16) << 8 | ((px(i + 3) >> 4) as u16) << 12
                    }
                };
                data.extend_from_slice(&w.to_be_bytes());
            }
        }
        self.reg_write(LISAR + 2, (self.addr >> 16) as u16)?;
        self.reg_write(LISAR, (self.addr & 0xffff) as u16)?;
        self.command(LD_IMG_AREA, &[bpp << 4, s.x as u16, s.y as u16, s.w as u16, s.h as u16])?;
        self.exchange(PRE_WRITE, &[], Some(&data), 0)?;
        self.command(LD_IMG_END, &[])?;
        Ok(s)
    }

    /// Refresh a panel rectangle seen from the front.
    pub fn display(&mut self, r: &Rect, mode: Mode) -> io::Result<()> {
        self.wake()?;
        let s = if self.mirrored { Rect::new(self.w - r.x - r.w, r.y, r.w, r.h) } else { *r };
        let m = self.mode_number(mode);
        self.command(DPY_AREA, &[s.x as u16, s.y as u16, s.w as u16, s.h as u16, m])
    }
}

impl Panel for It8951 {
    fn size(&self) -> (u32, u32) {
        (self.w, self.h)
    }

    fn reset(&mut self) -> io::Result<()> {
        self.start()
    }

    fn clear(&mut self) -> io::Result<()> {
        let all = Rect::new(0, 0, self.w, self.h);
        self.display(&all, Mode::Init)
    }

    fn push(&mut self, r: &Rect, grey: &[u8], mode: Mode) -> io::Result<()> {
        self.load(r, grey)?;
        self.display(r, mode)
    }

    fn sleep(&mut self) -> io::Result<()> {
        self.wait()?;
        self.command(SLEEP, &[])?;
        self.asleep = true;
        Ok(())
    }
}
