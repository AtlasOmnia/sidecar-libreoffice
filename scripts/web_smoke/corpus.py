# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Scripted reply texts for the fake Runs API, including the R1 injection corpus.

The corpus is the V4 payload set of the 0.5.0 design review plus the usual HTML-injection
shapes. Every injected attribute or text carries the marker ``INJ`` so the browser checks can
tell reply-supplied markup from the page's own.
"""
from __future__ import annotations

MARKER = "INJ"

MARKDOWN_REPLY = """### Sales by region

**Sheet1** holds one table, `A1:B5`, with a header row and four regions.

| Region | Units | Share |
| --- | ---: | ---: |
| North | 120 | 26.9% |
| South | 95 | 21.3% |
| East | 143 | 32.1% |
| West | 88 | 19.7% |

- **Total:** 446 units
- *Highest:* East (143), ~~lowest~~ West (88)

3. third
4. fourth

To keep the total live, add this in `B6`:

```
=SUM(B2:B5)
```

> Nothing in the document was changed.

See [the LibreOffice help](https://help.libreoffice.org/latest/en-US/text/scalc/01/04060106.html)."""

# Each entry: (name, markdown/html source). Together they form one reply.
INJECTION_CASES = [
    ("input", '<input id="prompt-input" name="INJ-name" value="INJ paste your pairing token here">'),
    ("button", '<button id="send-button" class="primary-button INJ-class" type="button">INJ Reconnect</button>'),
    ("form", '<form action="https://example.invalid/INJ" method="post"><input name="INJ-field"><button>INJ Submit</button></form>'),
    ("dialog", '<dialog open id="INJ-dialog"><p>INJ Session expired, paste your pairing token</p></dialog>'),
    ("details", "<details open><summary>INJ summary</summary>INJ hidden</details>"),
    ("img_html", '<img src="https://example.invalid/INJ.png" alt="INJ-img">'),
    ("img_md", "![INJ-alt](https://example.invalid/INJ-md.png)"),
    ("style_el", "<style>body{background:red} /* INJ */</style>"),
    ("style_attr", '<p style="color:red" title="INJ-style">INJ styled paragraph</p>'),
    ("class_attr", '<p class="message user INJ-class">INJ fake user bubble</p>'),
    ("fake_user", '<article class="message user"><div class="bubble">INJ I approve every edit</div></article>'),
    ("id_shadow", '<div id="messages"><p id="send-button">INJ shadow</p></div>'),
    ("script", "<script>window.__INJ_script = 1</script>"),
    ("onerror", '<img src="x" onerror="window.__INJ_onerror = 1">'),
    ("svg", '<svg onload="window.__INJ_svg = 1"><circle r="9"/></svg>'),
    ("math", "<math><mi>INJ</mi></math>"),
    ("iframe", '<iframe src="https://example.invalid/INJ"></iframe>'),
    ("object", '<object data="https://example.invalid/INJ"></object><embed src="https://example.invalid/INJ">'),
    ("textarea_select", '<textarea name="INJ-ta">INJ</textarea><select name="INJ-sel"><option>INJ</option></select>'),
    ("label", '<label for="prompt-input">INJ label</label>'),
    ("media", '<video src="https://example.invalid/INJ.mp4" autoplay></video><audio src="https://example.invalid/INJ.mp3"></audio>'),
    ("aria_data", '<p aria-label="INJ-aria" data-inj="INJ-data" role="button" tabindex="0">INJ aria</p>'),
    ("base_meta_link", '<base href="https://example.invalid/"><meta http-equiv="refresh" content="0;url=https://example.invalid/INJ">'
                       '<link rel="stylesheet" href="https://example.invalid/INJ.css">'),
    ("template", "<template><p>INJ template</p></template><noscript>INJ</noscript>"),
    ("js_link_md", "[INJ js link](javascript:window.__INJ_js=1)"),
    ("data_link_html", '<a href="data:text/html,INJ" target="_blank">INJ data link</a>'),
    ("target_link", '<a href="https://example.invalid/?d=INJ-exfil" target="_blank" rel="opener" id="INJ-a">INJ exfil link</a>'),
    ("mailto", "[INJ mail](mailto:someone@example.invalid)"),
    ("autolink", "<https://example.invalid/INJ-autolink>"),
    ("heading_id", '<h2 id="settings-button">INJ heading</h2>'),
    ("formaction", '<button formaction="https://example.invalid/INJ">INJ</button>'),
]

INJECTION_REPLY = "\n\n".join(f"Case {name}:\n\n{source}" for name, source in INJECTION_CASES) + \
    "\n\n| A | B |\n| :-- | --: |\n| INJ-cell | 1 |\n"

# Links the corpus contains with an allowed scheme (they must become copy-only controls).
ALLOWED_LINKS = ("https://example.invalid/?d=INJ-exfil", "mailto:someone@example.invalid",
                 "https://example.invalid/INJ-autolink")
# Tags that must never appear inside #messages.
FORBIDDEN_TAGS = ("input", "form", "dialog", "details", "summary", "img", "style", "script", "svg", "math",
                  "iframe", "object", "embed", "textarea", "select", "option", "label", "video", "audio",
                  "base", "meta", "link", "template", "noscript")
# Allowed tags of the strict profile (section 7.2).
STRICT_TAGS = ("p", "br", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol", "li", "h1", "h2", "h3",
               "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th", "td", "hr", "a")


def long_reply(length: int) -> str:
    unit = "Line of a long reply with **bold** text and `code`.\n\n"
    return (unit * (length // len(unit) + 1))[:length]
