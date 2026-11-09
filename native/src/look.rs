//! Colours, type and small drawn icons. Light and dark follow the system, after the page's
//! Siri-like look: blue bubbles for you, grey for the model, dark glass cards for pictures.

use rune::{Color, El, LineCap, Path, Shape, TextStyle, canvas};

#[derive(Clone, Copy)]
pub struct Pal {
    pub dark: bool,
    pub bg: Color,
    pub bar: Color,
    pub bubble: Color,
    pub mine: Color,
    pub text: Color,
    pub text2: Color,
    pub text3: Color,
    pub line: Color,
    pub control: Color,
    pub control_hover: Color,
    pub glass: Color,
    pub on_glass: Color,
    pub red: Color,
    pub green: Color,
    pub amber: Color,
    pub live_bg: Color,
}

/// sRGB-encoded 0..255 channels to Rune's linear-light colour.
pub fn lin(c: u8) -> f32 {
    let c = c as f32 / 255.0;
    if c <= 0.04045 { c / 12.92 } else { ((c + 0.055) / 1.055).powf(2.4) }
}

pub fn rgb(r: u8, g: u8, b: u8) -> Color {
    Color { r: lin(r), g: lin(g), b: lin(b), a: 1.0 }
}

pub fn rgba(r: u8, g: u8, b: u8, a: f32) -> Color {
    Color { a, ..rgb(r, g, b) }
}

pub fn pal(dark: bool) -> Pal {
    if dark {
        Pal {
            dark,
            bg: rgb(28, 28, 30),
            bar: rgb(36, 36, 39),
            bubble: rgb(44, 44, 47),
            mine: rgb(10, 132, 255),
            text: rgb(242, 242, 247),
            text2: rgb(152, 152, 159),
            text3: rgb(99, 99, 104),
            line: Color { r: 1.0, g: 1.0, b: 1.0, a: 0.09 },
            control: Color { r: 1.0, g: 1.0, b: 1.0, a: 0.08 },
            control_hover: Color { r: 1.0, g: 1.0, b: 1.0, a: 0.14 },
            glass: rgba(62, 62, 68, 0.96),
            on_glass: rgb(235, 235, 240),
            red: rgb(255, 69, 58),
            green: rgb(48, 209, 88),
            amber: rgb(255, 159, 10),
            live_bg: rgb(12, 12, 14),
        }
    } else {
        Pal {
            dark,
            bg: rgb(247, 247, 250),
            bar: rgb(255, 255, 255),
            bubble: rgb(233, 233, 237),
            mine: rgb(0, 122, 255),
            text: rgb(29, 29, 31),
            text2: rgb(110, 110, 115),
            text3: rgb(170, 170, 176),
            line: Color { r: 0.0, g: 0.0, b: 0.0, a: 0.08 },
            control: Color { r: 0.0, g: 0.0, b: 0.0, a: 0.05 },
            control_hover: Color { r: 0.0, g: 0.0, b: 0.0, a: 0.09 },
            glass: rgba(38, 38, 42, 0.94),
            on_glass: rgb(235, 235, 240),
            red: rgb(255, 59, 48),
            green: rgb(40, 167, 69),
            amber: rgb(214, 128, 0),
            live_bg: rgb(250, 250, 252),
        }
    }
}

pub const BODY: TextStyle = TextStyle::new(14.0, 400.0).lh(20.0);
pub const BODY_BOLD: TextStyle = TextStyle::new(14.0, 600.0).lh(20.0);
pub const SMALL: TextStyle = TextStyle::new(12.0, 400.0).lh(16.0);
pub const SMALL_BOLD: TextStyle = TextStyle::new(12.0, 600.0).lh(16.0);
pub const LABEL: TextStyle = TextStyle::new(13.0, 500.0).lh(18.0);
pub const TITLE: TextStyle = TextStyle::new(14.0, 650.0).lh(20.0);
pub const HEADLINE: TextStyle = TextStyle::new(22.0, 650.0).lh(28.0);
pub const LIVE_WORDS: TextStyle = TextStyle::new(28.0, 600.0).lh(35.0);
pub const LIVE_CAPTION: TextStyle = TextStyle::new(17.0, 400.0).lh(24.0);

#[derive(Clone, Copy)]
pub enum Glyph {
    Plus,
    Mic,
    Up,
    Stop,
    Play,
    Close,
    MicOff,
    Compose,
    Film,
    Photo,
    Wave,
}

/// A small line icon, drawn in a 20×20 box scaled to `size`.
pub fn glyph(g: Glyph, size: f32, color: Color) -> El {
    let w = 1.7;
    let round = LineCap::Round;
    let shape = match g {
        Glyph::Plus => Shape::new().line([10.0, 4.0], [10.0, 16.0], w, round, color).line([4.0, 10.0], [16.0, 10.0], w, round, color),
        Glyph::Close => Shape::new().line([5.5, 5.5], [14.5, 14.5], w, round, color).line([14.5, 5.5], [5.5, 14.5], w, round, color),
        Glyph::Up => Shape::new()
            .line([10.0, 16.0], [10.0, 4.5], 2.0, round, color)
            .line([5.0, 9.0], [10.0, 4.0], 2.0, round, color)
            .line([15.0, 9.0], [10.0, 4.0], 2.0, round, color),
        Glyph::Stop => Shape::new().fill(vec![[5.5, 5.5], [14.5, 5.5], [14.5, 14.5], [5.5, 14.5]], color),
        Glyph::Play => Shape::new().fill(vec![[6.5, 4.5], [16.0, 10.0], [6.5, 15.5]], color),
        Glyph::Mic | Glyph::MicOff => {
            let cup = Path::new().move_to([5.0, 9.5]).cubic_to([5.0, 16.0], [15.0, 16.0], [15.0, 9.5]);
            let mut s = Shape::new()
                .line([10.0, 4.0], [10.0, 9.5], 5.0, round, color)
                .stroke_path(&cup, w, round, color)
                .line([10.0, 14.6], [10.0, 17.0], w, round, color);
            if matches!(g, Glyph::MicOff) {
                s = s.line([4.0, 3.5], [16.0, 17.0], w, round, color);
            }
            s
        }
        Glyph::Film => Shape::new()
            .stroke(vec![[3.5, 5.0], [16.5, 5.0], [16.5, 15.0], [3.5, 15.0], [3.5, 5.0]], w, color)
            .fill(vec![[8.5, 7.5], [13.0, 10.0], [8.5, 12.5]], color),
        Glyph::Photo => Shape::new()
            .stroke(vec![[3.5, 4.5], [16.5, 4.5], [16.5, 15.5], [3.5, 15.5], [3.5, 4.5]], w, color)
            .stroke(vec![[4.5, 14.0], [9.0, 9.5], [12.0, 12.0], [13.5, 10.5], [16.0, 13.0]], w, color)
            .line([12.6, 7.4], [12.61, 7.4], 2.4, round, color),
        Glyph::Wave => Shape::new()
            .line([4.0, 8.5], [4.0, 11.5], w, round, color)
            .line([7.0, 6.0], [7.0, 14.0], w, round, color)
            .line([10.0, 3.5], [10.0, 16.5], w, round, color)
            .line([13.0, 6.5], [13.0, 13.5], w, round, color)
            .line([16.0, 8.5], [16.0, 11.5], w, round, color),
        Glyph::Compose => Shape::new()
            .stroke(vec![[9.0, 4.5], [4.5, 4.5], [4.5, 15.5], [15.5, 15.5], [15.5, 11.0]], w, color)
            .line([9.0, 11.0], [15.5, 4.5], w, round, color),
    };
    canvas(shape.view_box(20.0, 20.0)).size(size, size)
}
