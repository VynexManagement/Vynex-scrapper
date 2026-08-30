"""Synthetic storefronts encoding the exact failure modes the rewrite fixes.

These are regression fixtures, not a substitute for real ones. Real labelled
store fingerprints live in `tests/fixtures/*.json` and are captured with
`tools/capture_fixture.py`; `test_precision.py` measures precision against those.
"""

from typing import Optional

FILLER = "<div class='product-card'><p>Shop our bestselling collection today.</p></div>\n"


def make_store_html(
    *,
    script_srcs: Optional[list[str]] = None,
    inline_js: str = "",
    class_names: Optional[list[str]] = None,
    meta_description: Optional[str] = "A carefully curated shop with everything you need for the season ahead.",
    og_title: Optional[str] = "Northwind Supply",
    title: str = "Northwind Supply | Everyday Essentials",
    h1_count: int = 1,
    body_text: str = "",
    mailto: Optional[str] = None,
    social: Optional[list[str]] = None,
    theme_name: str = "Dawn",
    currency: str = "USD",
    shopify: bool = True,
    padding: int = 260,
) -> str:
    """Build a storefront large and script-rich enough to clear quality gating."""
    parts: list[str] = ["<!doctype html><html><head>"]
    parts.append(f"<title>{title}</title>")
    if meta_description is not None:
        parts.append(f'<meta name="description" content="{meta_description}">')
    if og_title is not None:
        parts.append(f'<meta property="og:title" content="{og_title}">')

    for src in script_srcs or []:
        parts.append(f'<script src="{src}"></script>')

    shopify_globals = ""
    if shopify:
        shopify_globals = (
            f'Shopify.theme = {{"name":"{theme_name}","id":12345,"theme_store_id":887}};\n'
            f'Shopify.currency = {{"active":"{currency}","rate":"1.0"}};\n'
            'Shopify.shop = "northwind-supply.myshopify.com";\n'
        )

    # Three baseline inline scripts so script_count clears the floor even for a
    # store with no third-party vendors at all.
    parts.append(f"<script>{shopify_globals}</script>")
    parts.append("<script>var theme = {strings:{addToCart:'Add to cart'}};</script>")
    parts.append("<script>window.performance && window.performance.mark('head');</script>")
    if inline_js:
        parts.append(f"<script>{inline_js}</script>")

    parts.append("</head><body>")
    for _ in range(max(0, h1_count)):
        parts.append("<h1>Northwind Supply</h1>")

    if class_names:
        for name in class_names:
            parts.append(f"<div class='{name}'>widget</div>")

    if body_text:
        parts.append(f"<p>{body_text}</p>")
    if mailto:
        parts.append(f'<a href="mailto:{mailto}">Email us</a>')
    for link in social or []:
        parts.append(f'<a href="{link}">Follow</a>')

    if shopify:
        parts.append('<img src="https://cdn.shopify.com/s/files/1/0001/logo.png">')
        parts.append('<link rel="canonical" href="https://northwind.example/">')

    parts.append(FILLER * padding)
    parts.append("</body></html>")
    return "\n".join(parts)


WEB_PIXELS_INLINE = (
    'window.webPixelsManagerAPI = {};'
    'var config = {"webPixelsConfigList":['
    '{"id":"101","configuration":"{}","eventPayloadVersion":"v1",'
    '"runtimeContext":"STRICT","scriptVersion":"1","type":"APP","name":"Facebook Pixel"},'
    '{"id":"102","configuration":"{}","eventPayloadVersion":"v1",'
    '"runtimeContext":"LAX","scriptVersion":"1","type":"APP","name":"Google Analytics"}'
    ']};'
)

EMPTY_WEB_PIXELS_INLINE = 'var config = {"webPixelsConfigList":[]};'

CHALLENGE_HTML = (
    "<!doctype html><html><head><title>Just a moment...</title></head>"
    "<body><div class='cf-browser-verification'>Checking your browser before accessing.</div>"
    "</body></html>"
)
