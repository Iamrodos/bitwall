//! What the machine says of itself, each platform its own way: the
//! process's CPU time, when the machine started, its name, the time on its
//! clock, and its address on the network.

use std::time::{SystemTime, UNIX_EPOCH};

pub fn unix_now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// The address a packet to the internet would leave from; none is sent.
pub fn address() -> String {
    std::net::UdpSocket::bind("0.0.0.0:0")
        .and_then(|s| {
            s.connect("192.0.2.1:9")?;
            s.local_addr()
        })
        .map(|a| a.ip().to_string())
        .unwrap_or_default()
}

const DAYS: [&str; 7] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/// "Tue 13 Oct 16:49", with the seconds where asked, as every receiver's
/// status page writes it.
pub fn clock(seconds: bool) -> String {
    let (wd, d, mo, h, mi, s) = local();
    let mut out = format!("{} {:02} {} {:02}:{:02}", DAYS[wd % 7], d, MONTHS[(mo + 11) % 12], h, mi);
    if seconds {
        out += &format!(":{s:02}");
    }
    out
}

#[cfg(windows)]
mod imp {
    use windows_sys::Win32::Foundation::FILETIME;
    use windows_sys::Win32::System::SystemInformation::*;
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, GetProcessTimes};

    pub fn cpu_seconds() -> f64 {
        unsafe {
            let z = FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };
            let (mut a, mut b, mut k, mut u) = (z, z, z, z);
            if GetProcessTimes(GetCurrentProcess(), &mut a, &mut b, &mut k, &mut u) == 0 {
                return 0.0;
            }
            let t = |f: FILETIME| ((f.dwHighDateTime as u64) << 32 | f.dwLowDateTime as u64) as f64 / 1e7;
            t(k) + t(u)
        }
    }

    pub fn booted() -> Option<u64> {
        Some(super::unix_now().saturating_sub(unsafe { GetTickCount64() } / 1000))
    }

    pub fn hostname() -> String {
        unsafe {
            let mut buf = [0u16; 256];
            let mut n = buf.len() as u32;
            if GetComputerNameExW(ComputerNameDnsHostname, buf.as_mut_ptr(), &mut n) != 0 {
                return String::from_utf16_lossy(&buf[..n as usize]);
            }
        }
        std::env::var("COMPUTERNAME").unwrap_or_default()
    }

    pub fn local() -> (usize, u32, usize, u32, u32, u32) {
        unsafe {
            let mut t = std::mem::zeroed();
            GetLocalTime(&mut t);
            let t: windows_sys::Win32::Foundation::SYSTEMTIME = t;
            (t.wDayOfWeek as usize, t.wDay as u32, t.wMonth as usize, t.wHour as u32, t.wMinute as u32, t.wSecond as u32)
        }
    }
}

#[cfg(unix)]
mod imp {
    pub fn cpu_seconds() -> f64 {
        unsafe {
            let mut u: libc::rusage = std::mem::zeroed();
            libc::getrusage(libc::RUSAGE_SELF, &mut u);
            let t = |v: libc::timeval| v.tv_sec as f64 + v.tv_usec as f64 / 1e6;
            t(u.ru_utime) + t(u.ru_stime)
        }
    }

    pub fn booted() -> Option<u64> {
        if let Ok(stat) = std::fs::read_to_string("/proc/stat") {
            for line in stat.lines() {
                if let Some(v) = line.strip_prefix("btime ") {
                    return v.trim().parse().ok();
                }
            }
        }
        let out = std::process::Command::new("sysctl").args(["-n", "kern.boottime"]).output().ok()?;
        let text = String::from_utf8_lossy(&out.stdout);
        let at = text.find("sec = ")? + 6;
        text[at..].split(|c: char| !c.is_ascii_digit()).next()?.parse().ok()
    }

    pub fn hostname() -> String {
        let mut buf = [0u8; 256];
        if unsafe { libc::gethostname(buf.as_mut_ptr() as _, buf.len()) } != 0 {
            return String::new();
        }
        let n = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
        let name = String::from_utf8_lossy(&buf[..n]).into_owned();
        name.split('.').next().unwrap_or("").to_string()
    }

    pub fn local() -> (usize, u32, usize, u32, u32, u32) {
        unsafe {
            let now = libc::time(std::ptr::null_mut());
            let mut tm: libc::tm = std::mem::zeroed();
            libc::localtime_r(&now, &mut tm);
            (tm.tm_wday as usize, tm.tm_mday as u32, tm.tm_mon as usize + 1, tm.tm_hour as u32, tm.tm_min as u32, tm.tm_sec as u32)
        }
    }
}

pub use imp::*;
