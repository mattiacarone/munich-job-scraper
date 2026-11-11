"""7 Nov 2026: board badges (app stores, Product Hunt, social links) must never become the employer apply link."""
from intake_checks import find_employer_link, check_employer_link


def test_app_store_badge_is_not_an_employer_link():
    page = ('<a href="https://apps.apple.com/us/app/jobicy-remote-jobs-salaries/id6807843232">Get the app</a>'
            '<a href="https://canonical.com/careers/1234-product-manager">Apply now</a>')
    assert find_employer_link(page) == "https://canonical.com/careers/1234-product-manager"


def test_product_hunt_badge_alone_gives_no_link():
    assert find_employer_link('<a href="https://www.producthunt.com/posts/remote-ok-jobs-api">Product Hunt</a>') is None


def test_app_store_apply_url_fails_the_employer_link_check():
    ok, reason = check_employer_link("jobicy", "https://apps.apple.com/us/app/jobicy-remote-jobs-salaries/id1")
    assert ok is False and "apps.apple.com" in reason


def test_real_ats_link_still_found():
    assert find_employer_link("apply at https://job-boards.greenhouse.io/canonical/jobs/123") == "https://job-boards.greenhouse.io/canonical/jobs/123"


def test_registry_match_needs_exact_or_unique_near_title(monkeypatch):
    import types, intake_checks
    postings = [("Product Manager", "https://gh/canonical/1"), ("Junior Product Manager", "https://gh/canonical/2"), ("Manager, Product Manager", "https://gh/canonical/3")]
    monkeypatch.setattr(intake_checks, "_list_ats_board_postings", lambda ats, slug, client=None, db=None: postings)

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q): return [("greenhouse", "canonical", "Canonical")]
    db = types.SimpleNamespace(_get_connection=lambda: Conn())

    def job(title): return types.SimpleNamespace(company="Canonical", title=title, url="u", source="jobicy", ats=None, description="", apply_url=None)
    assert intake_checks.resolve_employer_link_via_registry(job("Product Manager"), db) == "https://gh/canonical/1"
    assert intake_checks.resolve_employer_link_via_registry(job("Junior Product Manager"), db) == "https://gh/canonical/2"
    assert intake_checks.resolve_employer_link_via_registry(job("Product Manager - Security"), db) is None
