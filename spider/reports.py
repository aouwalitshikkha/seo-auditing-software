"""Generate the client CSV reports and the summary from the store."""

import csv
from collections import defaultdict
from urllib.parse import urlparse, urlunparse

from spider.parse import OG_TAGS, _is_data_uri
from spider.status import classify
from spider.store import get_status, iter_images, iter_links, iter_pages

PAGE_COLUMNS = ["Page ID", "URL", "Status Code", "Meta Title?", "Title Duplicated?",
                "Meta Description?", "Meta Duplicated?", "Open Graph?", "Canonical?"]
LINK_COLUMNS = ["Issue Type", "Found On (Page ID)", "Found On URL", "Target URL",
                "Status Code", "Redirect Destination", "Hops"]
EXTERNAL_SUMMARY_COLUMNS = ["Status Code", "Target URL", "Destination URL",
                            "Pages Affected", "Example Page", "Note"]
IMAGE_ISSUE_COLUMNS = ["Issue Type", "Image URL", "Status Code",
                       "Pages Affected", "Example Page"]


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def is_internal(host: str, origin_host: str) -> bool:
    """Same-site test for <a> targets: the origin host or any subdomain of it."""
    return host == origin_host or host.endswith("." + origin_host)


def _last_two(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_geo_redirect(target_url: str, destination_url: str) -> bool:
    """True when a redirect only prepends a 2-letter country label to the same host
    (e.g. pinterest.com -> za.pinterest.com), i.e. a crawl-location geo-route, not a
    real destination change."""
    t = _host(target_url)
    d = _host(destination_url)
    if t.startswith("www."):
        t = t[4:]
    if not t or not d:
        return False
    label, _, rest = d.partition(".")
    if len(label) == 2 and label.isascii() and label.isalpha():
        return rest == t or rest == _last_two(t)
    return False


_DECORATIVE_PIXEL_HOSTS = {"stats.wp.com", "pixel.wp.com"}

_SHARE_HOSTS = {
    "facebook.com", "www.facebook.com",
    "linkedin.com", "www.linkedin.com",
    "pinterest.com", "www.pinterest.com",
    "tumblr.com", "www.tumblr.com",
    "twitter.com", "www.twitter.com", "x.com", "www.x.com",
    "reddit.com", "www.reddit.com",
    "wa.me", "api.whatsapp.com",
}


def _normalize_target(target: str) -> tuple[str, bool]:
    """Return (normalized_url, was_share). For known social share hosts, strip the
    query string so that share-button URLs differing only by the shared article URL
    collapse to one row (e.g. all facebook.com/sharer.php?u=... become one)."""
    host = _host(target)
    for share_host in _SHARE_HOSTS:
        if host == share_host or host.endswith("." + share_host):
            p = urlparse(target)
            normalized = urlunparse((p.scheme, p.netloc, p.path, None, None, None))
            return normalized, True
    return target, False


def _is_decorative_image(src: str) -> bool:
    """Avatars and tracking pixels are not content images and carry no meaningful
    alt text, so they are not image issues worth reporting."""
    h = _host(src)
    return (h == "gravatar.com" or h.endswith(".gravatar.com")
            or h in _DECORATIVE_PIXEL_HOSTS)


def _dup_counts(pages, key):
    groups = defaultdict(int)
    for p in pages:
        v = p[key]
        if v:
            groups[v] += 1
    return groups


def _og_status(conn, page):
    """Combined Open Graph verdict: flags a broken og:image and/or missing core
    tags, so a page's full OG state shows in one cell (avoids 'fix one, find the
    next next month')."""
    parts = []
    if page["og_image"]:
        st = get_status(conn, page["og_image"])
        if st and classify(st[0], st[1]) == "broken":
            parts.append("Broken og:image")
    missing = [t for t in OG_TAGS if t not in page["og_present"]]
    if missing:
        parts.append("Missing: " + ", ".join(missing))
    return "; ".join(parts) if parts else "OK"


def write_page_audit(conn, path: str) -> int:
    pages = list(iter_pages(conn))
    title_dups = _dup_counts(pages, "title")
    desc_dups = _dup_counts(pages, "description")
    written = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(PAGE_COLUMNS)
        for p in pages:
            if p["status"] != 200:
                continue  # non-200 (redirects/errors) are covered by link_issues
            title_missing = not p["title"]
            desc_missing = not p["description"]
            title_dup = p["title"] and title_dups[p["title"]] > 1
            desc_dup = p["description"] and desc_dups[p["description"]] > 1
            og = _og_status(conn, p)
            canonical_ok = bool(p["canonical_present"])
            if not any([title_missing, desc_missing, title_dup, desc_dup,
                        og != "OK", not canonical_ok]):
                continue
            w.writerow([
                p["page_id"], p["display_url"], p["status"],
                "Missing" if title_missing else "OK",
                f"Yes ({title_dups[p['title']]})" if title_dup else "No",
                "Missing" if desc_missing else "OK",
                f"Yes ({desc_dups[p['description']]})" if desc_dup else "No",
                og,
                "OK" if canonical_ok else "Missing",
            ])
            written += 1
    return written


def _issue_rows(conn):
    for kind, items, url_key in (("link", iter_links(conn), "target"),
                                 ("image", iter_images(conn), "src")):
        for it in items:
            target = it[url_key]
            st = get_status(conn, target)
            if not st:
                continue
            code, hops, final = st
            verdict = classify(code, hops)
            if verdict == "ok":
                continue
            if verdict == "redirected":
                issue = "Redirected"
                dest, hop_s = final, str(hops)
            else:
                issue = "Broken Link" if kind == "link" else "Broken Image"
                dest, hop_s = "", ""
            yield [issue, it["found_on_id"], it["found_on_url"], target,
                   str(code), dest, hop_s]
    # images with missing/empty alt text (an accessibility + SEO issue,
    # independent of whether the image itself loads)
    for img in iter_images(conn):
        if img["missing_alt"]:
            yield ["Missing Alt", img["found_on_id"], img["found_on_url"],
                   img["src"], "", "", ""]


def write_link_issues(conn, path: str) -> int:
    written = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(LINK_COLUMNS)
        for row in _issue_rows(conn):
            w.writerow(row)
            written += 1
    return written


def _collect(conn, origin):
    """Single classification pass -> (internal_rows, external, image) where:
      internal_rows: list of LINK_COLUMNS tuples, exact byte-duplicates removed.
      external: (groups, order); groups[key]={pages:set, example, note, verdict},
                key=(status_str, target, destination).
      image:    (groups, order); groups[key]={pages:set, example, code},
                key=(issue_type, src)."""
    origin_host = urlparse(origin).netloc.lower()
    internal, seen = [], set()
    ext, ext_order = {}, []
    img, img_order = {}, []

    for it in iter_links(conn):
        target = it["target"]
        st = get_status(conn, target)
        if not st:
            continue
        code, hops, final = st
        verdict = classify(code, hops)
        if verdict == "ok":
            continue
        if is_internal(_host(target), origin_host):
            if verdict == "redirected":
                issue, dest, hop_s = "Redirected", final, str(hops)
            else:
                issue, dest, hop_s = "Broken Link", "", ""
            row = (issue, it["found_on_id"], it["found_on_url"], target,
                   str(code), dest, hop_s)
            if row not in seen:
                seen.add(row)
                internal.append(row)
        else:
            normalized, was_share = _normalize_target(target)
            dest = final if verdict == "redirected" else ""
            key = (str(code), normalized, dest)
            if key not in ext:
                note_parts = []
                if was_share:
                    note_parts.append("share-button")
                if verdict == "redirected" and is_geo_redirect(target, final):
                    note_parts.append("geo-redirect")
                note = "; ".join(note_parts)
                ext[key] = {"pages": set(), "example": it["found_on_url"],
                            "note": note, "verdict": verdict}
                ext_order.append(key)
            ext[key]["pages"].add(it["found_on_url"])

    for im in iter_images(conn):
        src = im["src"]
        if _is_data_uri(src) or _is_decorative_image(src):
            continue
        st = get_status(conn, src)
        if st and classify(st[0], st[1]) == "broken":
            key = ("Broken Image", src)
            if key not in img:
                img[key] = {"pages": set(), "example": im["found_on_url"], "code": str(st[0])}
                img_order.append(key)
            img[key]["pages"].add(im["found_on_url"])
        if im["missing_alt"]:
            key = ("Missing Alt", src)
            if key not in img:
                img[key] = {"pages": set(), "example": im["found_on_url"], "code": ""}
                img_order.append(key)
            img[key]["pages"].add(im["found_on_url"])

    return internal, (ext, ext_order), (img, img_order)


def write_internal_link_issues(conn, path: str, origin: str) -> int:
    internal, _, _ = _collect(conn, origin)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(LINK_COLUMNS)
        for row in internal:
            w.writerow(row)
    return len(internal)


def write_external_link_summary(conn, path: str, origin: str) -> int:
    _, (ext, order), _ = _collect(conn, origin)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(EXTERNAL_SUMMARY_COLUMNS)
        for key in order:
            code, target, dest = key
            g = ext[key]
            w.writerow([code, target, dest, len(g["pages"]), g["example"], g["note"]])
    return len(order)


def write_image_issues(conn, path: str) -> int:
    # origin unused here: image classification is origin-independent
    _, _, (img, order) = _collect(conn, origin="")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(IMAGE_ISSUE_COLUMNS)
        for key in order:
            issue, src = key
            g = img[key]
            w.writerow([issue, src, g["code"], len(g["pages"]), g["example"]])
    return len(order)


def write_summary(conn, path: str, meta: dict) -> None:
    internal, (ext, ext_order), (img, img_order) = _collect(conn, meta["origin"])
    pages = list(iter_pages(conn))

    int_broken = sum(1 for r in internal if r[0] == "Broken Link")
    int_redir = sum(1 for r in internal if r[0] == "Redirected")
    ext_broken = sum(1 for k in ext_order if ext[k]["verdict"] == "broken")
    ext_redir = sum(1 for k in ext_order if ext[k]["verdict"] == "redirected")
    ext_geo = sum(1 for k in ext_order if "geo-redirect" in ext[k]["note"])
    img_broken = sum(1 for k in img_order if k[0] == "Broken Image")
    img_alt = sum(1 for k in img_order if k[0] == "Missing Alt")

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"Report:        {meta['report_code']}\n")
        f.write(f"Client:        {meta['client']}\n")
        f.write(f"Domain:        {meta['domain']}\n")
        f.write(f"Start URL:     {meta['start_url']}\n")
        f.write(f"Canonical:     {meta['origin']}\n")
        f.write(f"Started:       {meta['started']}\n")
        f.write(f"Finished:      {meta['finished']}\n")
        f.write(f"Resumed:       {meta['resumed']}\n")
        f.write(f"Pages crawled: {len(pages)}\n")
        f.write("\nLink & image issues:\n")
        f.write(f"  Internal link issues:  {len(internal):6d}  "
                f"(broken {int_broken}, redirected {int_redir})\n")
        f.write(f"  External problems:     {len(ext_order):6d}  "
                f"(broken {ext_broken}, redirects {ext_redir}; of which geo {ext_geo})\n")
        f.write(f"  Image issues:          {len(img_order):6d}  "
                f"(broken {img_broken}, missing alt {img_alt})\n")
