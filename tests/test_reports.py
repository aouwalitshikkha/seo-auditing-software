import csv
from spider.store import (connect, init_schema, save_page, save_link,
                          save_image, save_status)
from spider.reports import write_page_audit, write_link_issues, write_summary
from spider.reports import is_internal, is_geo_redirect, _normalize_target


def populated(tmp_path):
    conn = connect(str(tmp_path / "c.db"))
    init_schema(conn)
    # page A: missing description, dup title "Home", og:image broken
    save_page(conn, "C-1", "https://e.com/a", "https://e.com/a", 200,
              "Home", None, ["og:image"], "https://e.com/og.png", True)
    # page B: dup title "Home", everything else fine, no canonical
    save_page(conn, "C-2", "https://e.com/b", "https://e.com/b", 200,
              "Home", "desc b",
              ["og:title", "og:description", "og:image", "og:type", "og:url"],
              "https://e.com/ok.png", False)
    # page C: clean (should NOT appear in audit)
    save_page(conn, "C-3", "https://e.com/c", "https://e.com/c", 200,
              "Unique", "desc c",
              ["og:title", "og:description", "og:image", "og:type", "og:url"],
              "https://e.com/ok.png", True)
    save_status(conn, "https://e.com/og.png", 404, 0, "https://e.com/og.png")
    save_status(conn, "https://e.com/ok.png", 200, 0, "https://e.com/ok.png")
    # links
    save_link(conn, "C-1", "https://e.com/a", "https://e.com/dead")
    save_link(conn, "C-1", "https://e.com/a", "https://e.com/redir")
    save_link(conn, "C-1", "https://e.com/a", "https://e.com/fine")
    save_status(conn, "https://e.com/dead", 404, 0, "https://e.com/dead")
    save_status(conn, "https://e.com/redir", 200, 2, "https://e.com/final")
    save_status(conn, "https://e.com/fine", 200, 0, "https://e.com/fine")
    save_image(conn, "C-1", "https://e.com/a", "https://e.com/broke.jpg", False)
    save_status(conn, "https://e.com/broke.jpg", 500, 0, "https://e.com/broke.jpg")
    # image that loads fine (200) but has no alt text
    save_image(conn, "C-1", "https://e.com/a", "https://e.com/noalt.png", True)
    save_status(conn, "https://e.com/noalt.png", 200, 0, "https://e.com/noalt.png")
    return conn


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_page_audit_columns_and_filtering(tmp_path):
    conn = populated(tmp_path)
    out = tmp_path / "page_audit.csv"
    write_page_audit(conn, str(out))
    rows = read_csv(out)
    ids = {r["Page ID"]: r for r in rows}
    assert "C-3" not in ids  # clean page excluded
    assert "C-1" in ids and "C-2" in ids
    a = ids["C-1"]
    assert a["Meta Description?"] == "Missing"
    assert a["Title Duplicated?"] == "Yes (2)"
    assert a["Open Graph?"] == "Broken og:image; Missing: og:title, og:description, og:type, og:url"
    assert a["Canonical?"] == "OK"
    b = ids["C-2"]
    assert b["Canonical?"] == "Missing"
    assert b["Open Graph?"] == "OK"
    assert b["Meta Duplicated?"] == "No"


def test_page_audit_excludes_non_200_pages(tmp_path):
    conn = connect(str(tmp_path / "c.db"))
    init_schema(conn)
    save_page(conn, "C-9", "https://e.com/old", "https://e.com/old", 301,
              None, None, [], None, False)
    out = tmp_path / "page_audit.csv"
    write_page_audit(conn, str(out))
    rows = read_csv(out)
    assert all(r["Page ID"] != "C-9" for r in rows)


def test_link_issues_classification(tmp_path):
    conn = populated(tmp_path)
    out = tmp_path / "link_issues.csv"
    write_link_issues(conn, str(out))
    rows = read_csv(out)
    by_target = {r["Target URL"]: r for r in rows}
    assert by_target["https://e.com/dead"]["Issue Type"] == "Broken Link"
    assert by_target["https://e.com/dead"]["Status Code"] == "404"
    assert by_target["https://e.com/redir"]["Issue Type"] == "Redirected"
    assert by_target["https://e.com/redir"]["Redirect Destination"] == "https://e.com/final"
    assert by_target["https://e.com/redir"]["Hops"] == "2"
    assert by_target["https://e.com/broke.jpg"]["Issue Type"] == "Broken Image"
    assert by_target["https://e.com/noalt.png"]["Issue Type"] == "Missing Alt"
    assert "https://e.com/fine" not in by_target  # ok links excluded


def test_summary_counts_three_sheets(tmp_path):
    from spider.reports import write_summary
    conn = seeded(tmp_path)
    out = tmp_path / "summary.txt"
    write_summary(conn, str(out), {"report_code": "E-20260605",
                                   "client": "E", "domain": "e.com",
                                   "start_url": "https://e.com",
                                   "origin": "https://e.com",
                                   "started": "2026-06-05T09:00",
                                   "finished": "2026-06-05T09:05",
                                   "resumed": False})
    text = out.read_text(encoding="utf-8")
    assert "E-20260605" in text
    # internal: 2 redirected (old on P1+P2) + 1 broken (gone) = 3 rows
    assert "Internal link issues:" in text
    assert "broken 1" in text and "redirected 2" in text
    # external: pinterest(geo+share-button) + linkedin(share-button) + semantica = 3 distinct
    assert "External problems:" in text
    assert "redirects 2" in text
    assert "geo 1" in text
    # images: 1 broken + 1 missing alt
    assert "Image issues:" in text
    assert "missing alt 1" in text


def test_is_internal():
    assert is_internal("washingtonparent.com", "washingtonparent.com")
    assert is_internal("www.washingtonparent.com", "washingtonparent.com")
    assert is_internal("picks.washingtonparent.com", "washingtonparent.com")
    assert not is_internal("za.pinterest.com", "washingtonparent.com")
    assert not is_internal("i0.wp.com", "washingtonparent.com")
    assert not is_internal("washingtonparent.semantica.co.za", "washingtonparent.com")


def test_is_geo_redirect():
    assert is_geo_redirect("https://www.pinterest.com/washparent",
                           "https://za.pinterest.com/washparent")
    assert is_geo_redirect("https://pinterest.com/pin/create/button/?x=1",
                           "https://za.pinterest.com/pin/create/button/?x=1")
    assert not is_geo_redirect("https://www.linkedin.com/shareArticle?x",
                               "https://www.linkedin.com/uas/login?x")
    assert not is_geo_redirect("https://www.facebook.com/sharer.php?u=x",
                               "https://www.facebook.com/share_channel/?x")


def seeded(tmp_path):
    """Crawl store with origin https://e.com and a mix of internal/external link
    issues plus image issues, for the curated-sheet writers."""
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    # --- internal redirect e.com/old -> e.com/new, found on P1 (twice) and P2 ---
    save_link(conn, "P-1", "https://e.com/p1", "https://e.com/old")
    save_link(conn, "P-1", "https://e.com/p1", "https://e.com/old")  # byte-duplicate
    save_link(conn, "P-2", "https://e.com/p2", "https://e.com/old")
    save_status(conn, "https://e.com/old", 200, 1, "https://e.com/new")
    # --- internal broken e.com/gone 404, found on P1 ---
    save_link(conn, "P-1", "https://e.com/p1", "https://e.com/gone")
    save_status(conn, "https://e.com/gone", 404, 0, "https://e.com/gone")
    # --- internal OK link (excluded) ---
    save_link(conn, "P-1", "https://e.com/p1", "https://e.com/fine")
    save_status(conn, "https://e.com/fine", 200, 0, "https://e.com/fine")
    # --- external geo-redirect pinterest, found on P1 and P2 ---
    save_link(conn, "P-1", "https://e.com/p1", "https://www.pinterest.com/washparent")
    save_link(conn, "P-2", "https://e.com/p2", "https://www.pinterest.com/washparent")
    save_status(conn, "https://www.pinterest.com/washparent", 200, 1,
                "https://za.pinterest.com/washparent")
    # --- external share-button (same endpoint, different query) found on P1 and P2 ---
    save_link(conn, "P-1", "https://e.com/p1", "https://www.linkedin.com/shareArticle?url=https%3A%2F%2Fe.com%2Fp1")
    save_link(conn, "P-2", "https://e.com/p2", "https://www.linkedin.com/shareArticle?url=https%3A%2F%2Fe.com%2Fp2")
    save_status(conn, "https://www.linkedin.com/shareArticle?url=https%3A%2F%2Fe.com%2Fp1", 200, 1,
                "https://www.linkedin.com/uas/login?x")
    save_status(conn, "https://www.linkedin.com/shareArticle?url=https%3A%2F%2Fe.com%2Fp2", 200, 1,
                "https://www.linkedin.com/uas/login?x")
    # --- external broken semantica (connect error), found on P1 and P2 ---
    save_link(conn, "P-1", "https://e.com/p1", "https://semantica.co.za")
    save_link(conn, "P-2", "https://e.com/p2", "https://semantica.co.za")
    save_status(conn, "https://semantica.co.za", "ERR:ConnectError", 0, "https://semantica.co.za")
    # --- broken image (CDN-ish), found on P1 and P2 ---
    save_image(conn, "P-1", "https://e.com/p1", "https://e.com/broke.jpg", False)
    save_image(conn, "P-2", "https://e.com/p2", "https://e.com/broke.jpg", False)
    save_status(conn, "https://e.com/broke.jpg", 500, 0, "https://e.com/broke.jpg")
    # --- image missing alt, found on P1 ---
    save_image(conn, "P-1", "https://e.com/p1", "https://e.com/noalt.png", True)
    save_status(conn, "https://e.com/noalt.png", 200, 0, "https://e.com/noalt.png")
    # --- redirected image (must NOT appear on image sheet) ---
    save_image(conn, "P-1", "https://e.com/p1", "https://e.com/imgredir.png", False)
    save_status(conn, "https://e.com/imgredir.png", 200, 1, "https://e.com/final.png")
    conn.commit()
    return conn


def test_internal_link_issues_sheet(tmp_path):
    from spider.reports import write_internal_link_issues
    conn = seeded(tmp_path)
    out = tmp_path / "internal_link_issues.csv"
    write_internal_link_issues(conn, str(out), "https://e.com")
    rows = read_csv(out)
    # only internal targets
    targets = [r["Target URL"] for r in rows]
    assert all(t.startswith("https://e.com/") for t in targets)
    assert "https://www.pinterest.com/washparent" not in targets
    assert "https://semantica.co.za" not in targets
    # byte-duplicate dropped: e.com/old on P-1 appears once, on P-2 once => 2 rows
    old_rows = [r for r in rows if r["Target URL"] == "https://e.com/old"]
    assert len(old_rows) == 2
    assert {r["Found On (Page ID)"] for r in old_rows} == {"P-1", "P-2"}
    redir = next(r for r in old_rows if r["Found On (Page ID)"] == "P-1")
    assert redir["Issue Type"] == "Redirected"
    assert redir["Redirect Destination"] == "https://e.com/new"
    assert redir["Hops"] == "1"
    gone = next(r for r in rows if r["Target URL"] == "https://e.com/gone")
    assert gone["Issue Type"] == "Broken Link"
    assert gone["Status Code"] == "404"
    assert "https://e.com/fine" not in targets  # ok excluded


def test_external_link_summary_sheet(tmp_path):
    from spider.reports import write_external_link_summary
    conn = seeded(tmp_path)
    out = tmp_path / "external_link_summary.csv"
    write_external_link_summary(conn, str(out), "https://e.com")
    rows = read_csv(out)
    by_target = {r["Target URL"]: r for r in rows}
    # only external targets, no internal leakage
    assert "https://e.com/old" not in by_target
    pin = by_target["https://www.pinterest.com/washparent"]
    assert pin["Status Code"] == "200"
    assert pin["Destination URL"] == "https://za.pinterest.com/washparent"
    assert pin["Pages Affected"] == "2"
    assert pin["Example Page"] in {"https://e.com/p1", "https://e.com/p2"}
    assert pin["Note"] == "share-button; geo-redirect"
    li = by_target["https://www.linkedin.com/shareArticle"]
    assert li["Pages Affected"] == "2"   # collapsed by share-button normalization
    assert li["Note"] == "share-button"
    assert li["Destination URL"] == "https://www.linkedin.com/uas/login?x"
    sem = by_target["https://semantica.co.za"]
    assert sem["Status Code"] == "ERR:ConnectError"
    assert sem["Destination URL"] == ""
    assert sem["Pages Affected"] == "2"


def test_image_issues_sheet(tmp_path):
    from spider.reports import write_image_issues
    conn = seeded(tmp_path)
    out = tmp_path / "image_issues.csv"
    write_image_issues(conn, str(out))
    rows = read_csv(out)
    by_key = {(r["Issue Type"], r["Image URL"]): r for r in rows}
    broke = by_key[("Broken Image", "https://e.com/broke.jpg")]
    assert broke["Status Code"] == "500"
    assert broke["Pages Affected"] == "2"
    noalt = by_key[("Missing Alt", "https://e.com/noalt.png")]
    assert noalt["Status Code"] == ""
    assert noalt["Pages Affected"] == "1"
    # redirected image is not an image issue
    assert ("Broken Image", "https://e.com/imgredir.png") not in by_key
    assert ("Redirected", "https://e.com/imgredir.png") not in by_key


def test_image_issues_excludes_data_uri_and_decorative(tmp_path):
    from spider.reports import write_image_issues
    conn = connect(str(tmp_path / "d.db"))
    init_schema(conn)
    # data-URI placeholder resolved to a bogus on-site URL that 404s -> excluded
    durl = "https://e.com/image/svg+xml;base64,PHN2Zz4="
    save_image(conn, "P-1", "https://e.com/p1", durl, False)
    save_status(conn, durl, 404, 0, durl)
    # gravatar avatar missing alt -> decorative, excluded
    save_image(conn, "P-1", "https://e.com/p1", "https://secure.gravatar.com/avatar/abc?s=96", True)
    # tracking pixel missing alt -> excluded
    save_image(conn, "P-1", "https://e.com/p1", "https://stats.wp.com/pixel.gif", True)
    # real content image missing alt -> KEPT
    save_image(conn, "P-1", "https://e.com/p1", "https://e.com/wp-content/uploads/photo.jpg", True)
    # real broken content image -> KEPT
    dead = "https://e.com/wp-content/uploads/dead.jpg"
    save_image(conn, "P-1", "https://e.com/p1", dead, False)
    save_status(conn, dead, 404, 0, dead)
    conn.commit()
    out = tmp_path / "image_issues.csv"
    write_image_issues(conn, str(out))
    rows = read_csv(out)
    urls = {r["Image URL"] for r in rows}
    assert "https://e.com/wp-content/uploads/photo.jpg" in urls
    assert dead in urls
    assert not any(";base64," in u for u in urls)
    assert not any("gravatar.com" in u for u in urls)
    assert "https://stats.wp.com/pixel.gif" not in urls
    assert len(rows) == 2


def test_normalize_target_share_url():
    """Share-button URLs differing only by query param collapse to same key."""
    a, was_a = _normalize_target(
        "https://www.facebook.com/sharer.php?u=https%3A%2F%2Fe.com%2Fa")
    b, was_b = _normalize_target(
        "https://www.facebook.com/sharer.php?u=https%3A%2F%2Fe.com%2Fb")
    assert a == "https://www.facebook.com/sharer.php"
    assert b == "https://www.facebook.com/sharer.php"
    assert was_a is True
    assert was_b is True


def test_normalize_target_pinterest_share():
    url = "https://pinterest.com/pin/create/button/?url=https%3A%2F%2Fe.com%2Fa&media=x&description=y"
    normalized, was = _normalize_target(url)
    assert normalized == "https://pinterest.com/pin/create/button/"
    assert was is True


def test_normalize_target_regular_url():
    """Regular external URLs are not modified."""
    url = "https://example.com/page?query=1"
    normalized, was = _normalize_target(url)
    assert normalized == url
    assert was is False


def test_normalize_target_linkedin_share():
    a, was_a = _normalize_target(
        "https://www.linkedin.com/shareArticle?mini=true&url=https%3A%2F%2Fe.com%2Fa")
    b, was_b = _normalize_target(
        "https://www.linkedin.com/shareArticle?mini=true&url=https%3A%2F%2Fe.com%2Fb")
    assert a == "https://www.linkedin.com/shareArticle"
    assert a == b
    assert was_a is True


def test_normalize_target_twitter_share():
    normalized, was = _normalize_target(
        "https://twitter.com/intent/tweet?text=Hello&url=https%3A%2F%2Fe.com%2Fa")
    assert normalized == "https://twitter.com/intent/tweet"
    assert was is True
