//! bitwall-eink: a wall on electronic paper.
//!
//! The wall composes everything it holds itself -- the base's cells, cells
//! layers above them, and pictures among those layers -- into sixteen
//! greys, and decides for itself when to push what to the panel and how,
//! since a panel that takes half a second to refresh and wears with every
//! refresh cannot simply paint what it is sent.
//!
//! | module | what |
//! |---|---|
//! | `scene` | what the receiver core hands over: cells, layers, pictures |
//! | `wall` | `Eink`, which the core calls: show, repaint, watching, tick |
//! | `compose` | the scene into greys, and the boxes that changed |
//! | `face` | the font, its cell at a size, its glyphs |
//! | `shapes` | the characters drawn as geometry, as the painter draws them |
//! | `grey` | colour to grey, paper, dithering |
//! | `geom` | rectangles, and the glass turned onto the panel |
//! | `policy` | when to push and in which mode |
//! | `panel` | where pushes go: a directory of PNGs, or the IT8951 |
//! | `tune` | every number the above decide by |

pub mod compose;
pub mod face;
pub mod geom;
pub mod grey;
pub mod panel;
pub mod policy;
pub mod scene;
pub mod shapes;
pub mod tune;
pub mod wall;

pub use geom::Rect;
pub use grey::Dither;
pub use panel::{FilePanel, Panel};
pub use policy::Mode;
pub use scene::{Cell, Cells, Layer, Picture, Scene, Show};
pub use wall::{Eink, Options, Pushed};
