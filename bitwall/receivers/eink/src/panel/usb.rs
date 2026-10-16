//! The same IT8951 board on USB, its interface switch ignored: the board
//! is a USB mass-storage device, and every command is a vendor SCSI
//! command (0xFE) in a bulk-only transport wrapper, with its arguments
//! big-endian words in the data that follows. Pixels go a byte each, in
//! bands of at most 60 KiB, to the image buffer the board names.
//!
//! The operating system claims the board as a disk, so it is taken off
//! that driver first, which needs root on a Mac, and a process in a login
//! session: a launchd daemon is refused the board. Given back on close.
//! VCOM is set every start, since the board forgets it. The panel scans
//! mirrored as seen from the front, so every row is loaded right to left,
//! as the SPI driver loads it.

use std::io;
use std::time::Duration;

use rusb::{DeviceHandle, Direction, GlobalContext, TransferType};

use super::Panel;
use crate::geom::Rect;
use crate::policy::Mode;
use crate::tune::{USB_CHUNK, VCOM_MV};

const VID: u16 = 0x048d;
const PID: u16 = 0x8951;
const TIMEOUT: Duration = Duration::from_secs(10);

const GET_SYS: u8 = 0x80;
const DPY_AREA: u8 = 0x94;
const LD_IMAGE_AREA: u8 = 0xa2;
const PMIC: u8 = 0xa3;

fn err<E: std::fmt::Display>(what: &str) -> impl FnOnce(E) -> io::Error + '_ {
    move |e| io::Error::other(format!("{what}: {e}"))
}

/// A vendor command block: the board's signature, the opcode at 6, its
/// arguments after. System info answers zeros without the signature.
fn vendor(op: u8, args: &[u8]) -> [u8; 16] {
    let mut c = [0u8; 16];
    c[0] = 0xfe;
    c[2..6].copy_from_slice(b"8951");
    c[6] = op;
    c[7..7 + args.len()].copy_from_slice(args);
    c
}

fn words(ws: &[u32]) -> Vec<u8> {
    ws.iter().flat_map(|w| w.to_be_bytes()).collect()
}

pub struct Usb {
    dev: DeviceHandle<GlobalContext>,
    ep_in: u8,
    ep_out: u8,
    tag: u32,
    pub w: u32,
    pub h: u32,
    addr: u32,
    pub version: u32,
    /// The panel is mirrored as it scans.
    pub mirrored: bool,
    vcom_mv: u16,
}

impl Usb {
    /// Find the board, take it from the system's driver, read what it is,
    /// and set VCOM (`vcom_mv` millivolts below zero; None for this
    /// panel's own).
    pub fn open(vcom_mv: Option<u16>) -> io::Result<Usb> {
        let dev = rusb::open_device_with_vid_pid(VID, PID).ok_or_else(|| io::Error::other("no IT8951 on USB (048d:8951)"))?;
        if dev.kernel_driver_active(0).unwrap_or(false) {
            dev.detach_kernel_driver(0).map_err(err("taking the board from the system's driver (root?)"))?;
        }
        dev.claim_interface(0).map_err(err("claim (root, in a login session?)"))?;
        let config = dev.device().active_config_descriptor().map_err(err("config"))?;
        let (mut ep_in, mut ep_out) = (0, 0);
        for i in config.interfaces() {
            for d in i.descriptors().filter(|d| d.interface_number() == 0) {
                for e in d.endpoint_descriptors().filter(|e| e.transfer_type() == TransferType::Bulk) {
                    match e.direction() {
                        Direction::In => ep_in = e.address(),
                        Direction::Out => ep_out = e.address(),
                    }
                }
            }
        }
        if ep_in == 0 || ep_out == 0 {
            return Err(io::Error::other("the board has no bulk endpoints"));
        }
        let mut p = Usb { dev, ep_in, ep_out, tag: 0, w: 0, h: 0, addr: 0, version: 0, mirrored: true, vcom_mv: vcom_mv.unwrap_or(VCOM_MV) };
        p.start()?;
        Ok(p)
    }

    /// Read what the board is and set VCOM. As it is opened, and again
    /// whenever it stops answering.
    fn start(&mut self) -> io::Result<()> {
        let info = self.command(&vendor(GET_SYS, &[0, 1, 0, 2]), 112, &[])?;
        let word = |i: usize| u32::from_be_bytes(info[i * 4..i * 4 + 4].try_into().unwrap());
        if &info[8..12] != b"8951" {
            return Err(io::Error::other(format!("the board signs itself {:?}, not 8951", &info[8..12])));
        }
        self.version = word(3);
        self.w = word(4);
        self.h = word(5);
        self.addr = word(7);
        let v = self.vcom_mv.to_be_bytes();
        self.command(&vendor(PMIC, &[v[0], v[1], 1, 1, 1]), 0, &[])?;
        Ok(())
    }

    /// One command: its block, then data in or out, then the status.
    fn command(&mut self, cdb: &[u8; 16], data_in: usize, data_out: &[u8]) -> io::Result<Vec<u8>> {
        self.tag = self.tag.wrapping_add(1);
        let len = if data_in > 0 { data_in } else { data_out.len() } as u32;
        let mut cbw = Vec::with_capacity(31);
        cbw.extend_from_slice(b"USBC");
        cbw.extend_from_slice(&self.tag.to_le_bytes());
        cbw.extend_from_slice(&len.to_le_bytes());
        cbw.push(if data_in > 0 { 0x80 } else { 0 });
        cbw.push(0);
        cbw.push(16);
        cbw.extend_from_slice(cdb);
        self.dev.write_bulk(self.ep_out, &cbw, TIMEOUT).map_err(err("usb write"))?;
        let mut reply = vec![0u8; data_in];
        if data_in > 0 {
            let n = self.dev.read_bulk(self.ep_in, &mut reply, TIMEOUT).map_err(err("usb read"))?;
            reply.truncate(n);
        } else if !data_out.is_empty() {
            self.dev.write_bulk(self.ep_out, data_out, TIMEOUT).map_err(err("usb write"))?;
        }
        let mut csw = [0u8; 13];
        self.dev.read_bulk(self.ep_in, &mut csw, TIMEOUT).map_err(err("usb status"))?;
        if &csw[0..4] != b"USBS" || csw[4..8] != self.tag.to_le_bytes() || csw[12] != 0 {
            return Err(io::Error::other(format!("command {:02x} failed, status {}", cdb[6], csw[12])));
        }
        Ok(reply)
    }

    /// As the panel scans: mirrored rows, so the box moves across.
    fn scanned(&self, r: &Rect) -> Rect {
        if self.mirrored {
            Rect::new(self.w - r.x - r.w, r.y, r.w, r.h)
        } else {
            *r
        }
    }

    /// Load greys for a panel rectangle seen from the front.
    pub fn load(&mut self, r: &Rect, grey: &[u8]) -> io::Result<()> {
        let s = self.scanned(r);
        let w = r.w as usize;
        let band = ((USB_CHUNK - 20) / w).max(1);
        let mut top = 0;
        while top < r.h as usize {
            let n = band.min(r.h as usize - top);
            let mut data = words(&[self.addr, s.x, s.y + top as u32, s.w, n as u32]);
            for row in grey[top * w..(top + n) * w].chunks(w) {
                if self.mirrored {
                    data.extend(row.iter().rev());
                } else {
                    data.extend_from_slice(row);
                }
            }
            self.command(&vendor(LD_IMAGE_AREA, &[]), 0, &data)?;
            top += n;
        }
        Ok(())
    }

    /// Refresh a panel rectangle seen from the front. The board answers at
    /// once and takes no other command until the refresh is done.
    pub fn display(&mut self, r: &Rect, mode: Mode) -> io::Result<()> {
        let s = self.scanned(r);
        let m = match mode {
            Mode::Init => 0,
            Mode::Du => 1,
            Mode::Gc16 => 2,
        };
        let data = words(&[self.addr, m, s.x, s.y, s.w, s.h, 1]);
        self.command(&vendor(DPY_AREA, &[]), 0, &data).map(|_| ())
    }
}

impl Drop for Usb {
    fn drop(&mut self) {
        let _ = self.dev.release_interface(0);
        let _ = self.dev.attach_kernel_driver(0);
    }
}

impl Panel for Usb {
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
}
