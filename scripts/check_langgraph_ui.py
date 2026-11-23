"""Read-only browser checks of the real existing UI and persisted LangGraph demo.

Run with the existing Python Playwright environment; no API responses are mocked.
The deterministic demo must have been ingested first. Never invokes an LLM.
"""

import argparse
import json
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", type=Path, default=Path(".local/langgraph-v1/demo-result.json"))
    parser.add_argument("--output", type=Path, default=Path(".local/langgraph-v1/ui"))
    parser.add_argument("--frontend", default="http://localhost:5173")
    parser.add_argument("--api", default="http://localhost:8000/api/v1")
    args = parser.parse_args()
    run_id = json.loads(args.demo.read_text(encoding="utf-8"))["run_id"]
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as playwright:
        request = playwright.request.new_context()
        run_response = request.get(f"{args.api}/runs/{run_id}")
        assert run_response.status == 200
        run = run_response.json()
        assert run["source"] == "langgraph" and run["analysis_status"] == "review_required"
        assert run["normalized_step_count"] == 5
        assert run["validation_id"] is None
        browser = playwright.chromium.launch(headless=True)
        for width in (1440, 390):
            context = browser.new_context(viewport={"width": width, "height": 1000})
            context.route("https://fonts.googleapis.com/**", lambda route: route.abort())
            page = context.new_page()
            errors, responses, failures = [], [], []
            page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
            page.on(
                "response",
                lambda response, responses=responses: (
                    responses.append(
                        {
                            "url": response.url,
                            "status": response.status,
                            "method": response.request.method,
                        }
                    )
                    if response.url.startswith(args.api)
                    else None
                ),
            )
            page.on(
                "requestfailed",
                lambda failed, failures=failures: (
                    failures.append(
                        {
                            "url": failed.url,
                            "failure": failed.failure,
                        }
                    )
                    if failed.url.startswith(args.api)
                    else None
                ),
            )
            page.goto(args.frontend + "/dashboard")
            expect(page.get_by_role("link", name=run_id, exact=True).first).to_be_visible()
            expect(page.locator(".run-source").filter(has_text="langgraph").first).to_be_visible()
            page.screenshot(path=str(args.output / f"dashboard-{width}.png"), full_page=True)
            page.goto(args.frontend + "/runs/" + run_id)
            expect(page.locator(".run-header h1")).to_have_text(run_id)
            expect(page.locator(".run-header-meta .status")).to_have_text("Needs review")
            expect(
                page.get_by_role("heading", name="Eligibility Decision Agent", exact=True)
            ).to_be_visible()
            expect(page.locator(".failure-explanation")).to_contain_text("no LLM inference")
            if width == 390:
                page.get_by_role("button", name="Trace", exact=True).click()
            expect(page.locator(".trace-row")).to_have_count(5)
            expect(page.locator("#trace-step-0 .trace-agent")).to_have_text("Intake Agent")
            expect(page.locator("#trace-step-1 .trace-agent")).to_have_text(
                "Policy Retrieval Agent"
            )
            expect(page.locator("#trace-step-3 .trace-agent")).to_have_text(
                "Eligibility Decision Agent"
            )
            page.get_by_role("button", name=re.compile("Decisive step 3")).first.click()
            expect(page.get_by_label("Step 3 output")).to_contain_text('"approved": true')
            page.locator("#trace-step-3").get_by_text("Inputs received", exact=True).click()
            expect(page.get_by_label("Step 3 input")).to_contain_text('"days_since_purchase": 21')
            expect(page.get_by_label("Step 3 input")).to_contain_text('"refund_window_days": 14')
            page.locator("#trace-step-2 > button").click()
            page.locator("#trace-step-2").get_by_text(
                "Complete canonical payload", exact=True
            ).click()
            expect(page.get_by_label("Step 2 canonical payload")).to_contain_text(
                "lookup_refund_policy"
            )
            page.locator("#trace-step-4 > button").click()
            expect(page.get_by_label("Step 4 output")).to_contain_text("Your refund is approved.")
            page.screenshot(path=str(args.output / f"trace-{width}.png"), full_page=True)
            page.get_by_label("Trace view").select_option("raw")
            expect(page.get_by_label("Original trace")).to_contain_text('"version": "langgraph-v1"')
            expect(page.get_by_label("Original trace")).to_contain_text("evaluator_failed")
            if width == 390:
                page.get_by_role("button", name="Stage details", exact=True).click()
            navigation = page.get_by_role("navigation", name="Stage detail views")
            navigation.get_by_role("button", name="Regression spec", exact=True).click()
            expect(
                page.get_by_role("heading", name="Regression scenario", exact=True)
            ).to_be_visible()
            expect(page.locator(".stage-panel .context-warning")).to_contain_text(
                "no approved replay harness"
            )
            pipeline = page.get_by_role("navigation", name="Run pipeline")
            expect(pipeline.get_by_role("button", name=re.compile("Validation"))).to_contain_text(
                "Pending"
            )
            navigation.get_by_role("button", name="Generated test", exact=True).click()
            expect(page.get_by_text("No generated test yet", exact=True)).to_be_visible()
            navigation.get_by_role("button", name="Validation", exact=True).click()
            expect(page.get_by_text("Validation pending", exact=True)).to_be_visible()
            assert page.get_by_role("heading", name="Valid regression", exact=True).count() == 0
            page.screenshot(path=str(args.output / f"review-{width}.png"), full_page=True)
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            assert not errors, errors
            assert responses and all(r["status"] == 200 and r["method"] == "GET" for r in responses)
            assert all(f["failure"] == "net::ERR_ABORTED" for f in failures), failures
            results.append(
                {
                    "width": width,
                    "run_id": run_id,
                    "page_errors": errors,
                    "api_responses": responses,
                    "navigation_cancellations": failures,
                }
            )
            context.close()
        browser.close()
        request.dispose()
    (args.output / "results.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Real dashboard/trace/review browser checks passed at 1440px and 390px: {run_id}")


if __name__ == "__main__":
    main()
