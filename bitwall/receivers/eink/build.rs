//! The receiver's version: the first twelve hex digits of the SHA-256 of
//! its source files one after another, as bitwall/receivers/__init__.py
//! works it out from `SOURCES`, so a controller holding the same files
//! works out the same version. The two lists are kept the same.

use sha2::{Digest, Sha256};

const SOURCES: [&str; 31] = [
    "Cargo.toml",
    "build.rs",
    "src/bin/bitwall-eink/main.rs",
    "src/bin/bitwall-eink/kitty.rs",
    "src/lib.rs",
    "src/compose.rs",
    "src/face.rs",
    "src/geom.rs",
    "src/grey.rs",
    "src/policy.rs",
    "src/scene.rs",
    "src/shapes.rs",
    "src/tune.rs",
    "src/wall.rs",
    "src/panel/mod.rs",
    "src/panel/it8951.rs",
    "../core-rs/Cargo.toml",
    "../core-rs/src/lib.rs",
    "../core-rs/src/glass.rs",
    "../core-rs/src/grid.rs",
    "../core-rs/src/pipe/mod.rs",
    "../core-rs/src/pipe/unix.rs",
    "../core-rs/src/pipe/windows.rs",
    "../core-rs/src/proto.rs",
    "../core-rs/src/sys.rs",
    "../core-rs/src/video.rs",
    "../core-rs/src/wall.rs",
    "../core-rs/src/decode.rs",
    "../pi/MesloLGSNerdFontMono-Regular.ttf",
    "fonts/MesloLGSNerdFontMono-Bold.ttf",
    "fonts/MesloLGSNerdFont-Regular.ttf",
];

fn main() {
    let mut h = Sha256::new();
    for f in SOURCES {
        println!("cargo:rerun-if-changed={f}");
        h.update(std::fs::read(f).unwrap_or_default());
    }
    let hex: String = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
    println!("cargo:rustc-env=BITWALL_VERSION={}", &hex[..12]);
}
