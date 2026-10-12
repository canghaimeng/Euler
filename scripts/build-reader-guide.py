from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

root = Path(__file__).resolve().parents[1]
for lang, suffix in [("en", ""), ("zh", ".zh")]:
    source = root / "docs" / f"DASHBOARD-GUIDE{suffix}.md"
    text = source.read_text(encoding="utf-8")
    # The HTML reader already has language buttons and a return-to-project link.
    text = re.sub(r"<!-- language-nav -->.*?<!-- /language-nav -->\n", "", text, flags=re.S)

    def documentation_link(match):
        url = urlsplit(match.group(1))
        if url.scheme or url.netloc or not url.path.endswith('.md'):
            return match.group(0)
        target = (source.parent / url.path).resolve().relative_to(root)
        destination = urlunsplit(('https', 'github.com',
                                 '/canghaimeng/Euler/blob/main/' + target.as_posix(),
                                 url.query, url.fragment))
        return '](' + destination + ')'

    text = re.sub(r'\]\(([^\s)]+)\)', documentation_link, text)
    (root / "euler/observability/static" / f"help.{lang}.md").write_text(
        text, encoding="utf-8")
