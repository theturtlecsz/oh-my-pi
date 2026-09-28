import { describe, expect, test } from "bun:test";
import {
	SOURCE_DELIVERED_LABEL,
	readRepoText,
	unresolvedLinks,
} from "./fixtures/programme-claims";

const MASTER_DOC_REL = "MASTER.md";

describe("programme claims: MASTER.md E1 checkpoint", () => {
	test("the line starting ## Frozen checkpoint — September 19, 2026 holds the source delivered label", async () => {
		const content = await readRepoText(MASTER_DOC_REL);
		const lines = content.split("\n");
		const checkpointLine = lines.find(l => l.startsWith("## Frozen checkpoint — September 19, 2026"));
		expect(checkpointLine).toBeDefined();
		expect(checkpointLine).toContain(SOURCE_DELIVERED_LABEL);
	});

	test("unproven acceptance claims are absent", async () => {
		const content = await readRepoText(MASTER_DOC_REL);
		expect(content).not.toContain("E1 installed lifecycle PASS");
		expect(content).not.toContain("This accepts the frozen E1");
	});

	test("E1 packet link on the approval line resolves", async () => {
		const content = await readRepoText(MASTER_DOC_REL);
		const lines = content.split("\n");
		const approvalLine = lines.find(l => l.includes("Owner-authorized E1 contract approval is committed at `0116304a"));
		expect(approvalLine).toBeDefined();
		expect(approvalLine).toContain("[E1 packet status](docs/programme/E1-CONTRACT-APPROVAL-PACKET.md)");
		const missing = unresolvedLinks(MASTER_DOC_REL, approvalLine!);
		expect(missing).toEqual([]);
	});

	test("line 19 clarifies external run recorded at host paths without in-repo ledger or check", async () => {
		const content = await readRepoText(MASTER_DOC_REL);
		const lines = content.split("\n");
		const line19 = lines[18];
		expect(line19).toContain("recorded at host paths");
		expect(line19).toContain("holds no Work Ledger record or installed check");
		expect(line19).toContain("frozen E1 installed controls/recovery/installation delivery only");
	});
});
