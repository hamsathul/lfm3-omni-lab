//! Just enough Markdown for a chat answer, as styled byte ranges in one wrapping text:
//! headings, bullets, **bold**, *italic*, `code` and [links](url) (shown as their text).

use crate::look::{self, Pal};
use rune::{El, Span, rich};

pub fn parse(src: &str) -> (String, Vec<Span>) {
    let mut out = String::with_capacity(src.len());
    let mut spans = Vec::new();
    let mut in_fence = false;
    for (n, raw) in src.lines().enumerate() {
        if n > 0 {
            out.push('\n');
        }
        let trimmed = raw.trim_start();
        if trimmed.starts_with("```") {
            in_fence = !in_fence;
            // Drop the fence line itself (and the newline before it, when there was one).
            if out.ends_with('\n') {
                out.pop();
            }
            continue;
        }
        if in_fence {
            let start = out.len();
            out.push_str(raw);
            spans.push(Span { mono: true, ..Span::new(start, out.len()) });
            continue;
        }
        let hashes = trimmed.chars().take_while(|&c| c == '#').count();
        if (1..=6).contains(&hashes) && trimmed[hashes..].starts_with(' ') {
            let start = out.len();
            inline(&trimmed[hashes + 1..], &mut out, &mut spans);
            spans.push(Span { weight: Some(680.0), ..Span::new(start, out.len()) });
            continue;
        }
        let indent = raw.len() - trimmed.len();
        if let Some(rest) = trimmed.strip_prefix("- ").or_else(|| trimmed.strip_prefix("* ")).or_else(|| trimmed.strip_prefix("+ ")) {
            out.push_str(&" ".repeat(indent.min(6)));
            out.push_str("•  ");
            inline(rest, &mut out, &mut spans);
            continue;
        }
        inline(raw, &mut out, &mut spans);
    }
    // Rune wants spans in order and not overlapping; nested styles were pushed inside-out.
    spans.sort_by_key(|s| (s.start, s.end));
    (out, flatten(spans))
}

fn flatten(spans: Vec<Span>) -> Vec<Span> {
    let mut points: Vec<u32> = spans.iter().flat_map(|s| [s.start, s.end]).collect();
    points.sort_unstable();
    points.dedup();
    let mut flat = Vec::new();
    for w in points.windows(2) {
        let (a, b) = (w[0], w[1]);
        let covering: Vec<&Span> = spans.iter().filter(|s| s.start <= a && s.end >= b).collect();
        if covering.is_empty() {
            continue;
        }
        let mut s = Span::new(a as usize, b as usize);
        for c in covering {
            s.weight = s.weight.or(c.weight);
            s.mono |= c.mono;
            s.italic |= c.italic;
            s.bg = s.bg.or(c.bg);
            if c.bg.is_some() {
                s.bg_pad = c.bg_pad;
            }
        }
        flat.push(s);
    }
    flat
}

fn inline(src: &str, out: &mut String, spans: &mut Vec<Span>) {
    let mut rest = src;
    while !rest.is_empty() {
        if let Some(r) = rest.strip_prefix("**").or_else(|| rest.strip_prefix("__")) {
            let mark = &rest[..2];
            if let Some(end) = r.find(mark) {
                let start = out.len();
                inline(&r[..end], out, spans);
                spans.push(Span { weight: Some(650.0), ..Span::new(start, out.len()) });
                rest = &r[end + 2..];
                continue;
            }
        }
        if let Some(r) = rest.strip_prefix('`') {
            if let Some(end) = r.find('`') {
                let start = out.len();
                out.push_str(&r[..end]);
                spans.push(Span { mono: true, bg: Some(look::rgba(127, 127, 127, 0.16)), bg_pad: 2.0, ..Span::new(start, out.len()) });
                rest = &r[end + 1..];
                continue;
            }
        }
        if let Some(r) = rest.strip_prefix('*') {
            // *italic*, but not a lone asterisk (5 * 3).
            if !r.starts_with(' ') {
                if let Some(end) = r.find('*') {
                    if end > 0 && !r[..end].ends_with(' ') {
                        let start = out.len();
                        inline(&r[..end], out, spans);
                        spans.push(Span { italic: true, ..Span::new(start, out.len()) });
                        rest = &r[end + 1..];
                        continue;
                    }
                }
            }
        }
        if let Some(r) = rest.strip_prefix('[') {
            if let Some(close) = r.find("](") {
                if let Some(end) = r[close + 2..].find(')') {
                    let start = out.len();
                    inline(&r[..close], out, spans);
                    spans.push(Span { underline: Some(look::rgba(127, 127, 127, 0.6)), ..Span::new(start, out.len()) });
                    rest = &r[close + 2 + end + 1..];
                    continue;
                }
            }
        }
        let ch = rest.chars().next().unwrap();
        out.push(ch);
        rest = &rest[ch.len_utf8()..];
    }
}

/// An answer's text as a styled, wrapping element.
pub fn rich_text(src: &str, p: &Pal) -> El {
    let (text, spans) = parse(src);
    rich(text, spans).font(look::BODY).color(p.text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bold_code_and_bullets_become_spans() {
        let (text, spans) = parse("The latest is **Python 3.15.0**.\n- run `uv` first\n# Done");
        assert_eq!(text, "The latest is Python 3.15.0.\n•  run uv first\nDone");
        let bold = &spans[0];
        assert_eq!(&text[bold.start as usize..bold.end as usize], "Python 3.15.0");
        assert_eq!(bold.weight, Some(650.0));
        let code = spans.iter().find(|s| s.mono).unwrap();
        assert_eq!(&text[code.start as usize..code.end as usize], "uv");
        assert!(spans.windows(2).all(|w| w[0].end <= w[1].start));
    }

    #[test]
    fn arithmetic_asterisks_stay() {
        let (text, spans) = parse("5 * 3 = 15 and 2 * 4 = 8");
        assert_eq!(text, "5 * 3 = 15 and 2 * 4 = 8");
        assert!(spans.is_empty());
    }

    #[test]
    fn nested_bold_italic_is_flattened() {
        let (text, spans) = parse("**very *nice* day**");
        assert_eq!(text, "very nice day");
        assert!(spans.windows(2).all(|w| w[0].end <= w[1].start));
        let nice = spans.iter().find(|s| &text[s.start as usize..s.end as usize] == "nice").unwrap();
        assert!(nice.italic && nice.weight == Some(650.0));
    }
}
