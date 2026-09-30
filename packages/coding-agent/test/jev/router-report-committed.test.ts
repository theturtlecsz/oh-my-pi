import { describe, expect, it } from "bun:test";
import * as path from "node:path";
import type { RouterMeasurementResults } from "../../../../docs/reports/jev-measurement/router-harness";
import { renderRouterReport } from "../../../../docs/reports/jev-measurement/run-router";

const REPO_ROOT = path.resolve(import.meta.dir, "../../../..");
const RESULTS_JSON = path.join(REPO_ROOT, "docs/reports/jev-router-results.json");
const TEMPLATE = path.join(REPO_ROOT, "docs/reports/jev-measurement/router-report-template.md");
const REPORT_MD = path.join(REPO_ROOT, "docs/reports/jev-router-measurement-report.md");

const SECTION_3_START = "### 3. Robomp issue pre-gate";
const SECTION_3_END = "## What is not measured";

function robompSection(markdown: string): string {
	const start = markdown.indexOf(SECTION_3_START);
	if (start === -1) throw new Error(`missing section header: ${SECTION_3_START}`);
	const end = markdown.indexOf(SECTION_3_END, start);
	if (end === -1) throw new Error(`missing section boundary: ${SECTION_3_END}`);
	return markdown.slice(start, end);
}

describe("committed Jev Router report", () => {
	it("renders section 3 of the committed JSON identically to the committed markdown, with no 0.0% for the empty robomp route", async () => {
		const results = (await Bun.file(RESULTS_JSON).json()) as RouterMeasurementResults;
		const template = await Bun.file(TEMPLATE).text();
		const committed = await Bun.file(REPORT_MD).text();

		expect(results.features.robomp_route.sampleSize).toBe(0);

		const rendered = renderRouterReport(results, template);
		const renderedSection3 = robompSection(rendered);
		const committedSection3 = robompSection(committed);

		expect(renderedSection3).toBe(committedSection3);
		expect(renderedSection3).not.toContain("0.0%");
		expect(renderedSection3).toContain("Sample size: 0 issues");
	});
});
