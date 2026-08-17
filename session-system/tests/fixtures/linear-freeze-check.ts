/**
 * linear-freeze-check.ts — subprocess fixture for linear-freeze.test.ts. Launched with
 * HOME pointed at a temp dir so node:os.homedir() (cached at process start under Bun)
 * binds KEY_FILE/FREEZE_FILE to the fixture home. fetch is stubbed: mutation-bodied
 * requests are counted, and every request rejects with a sentinel. Backend methods may
 * read before they mutate, so the contract asserted is "zero mutation fetches leave the
 * process while frozen", not "the first error mentions the fence".
 * argv[2]: "unfrozen" | "frozen"
 */
import { apiKey, createLinearBackend } from "../../extensions/workflow/linear.ts";

let mutationCalls = 0;
globalThis.fetch = ((_input: unknown, init?: { body?: unknown }) => {
	const body = typeof init?.body === "string" ? (JSON.parse(init.body) as { query?: string }) : {};
	if (body.query && /^\s*mutation\b/.test(body.query)) mutationCalls += 1;
	return Promise.reject(new Error("fetch_sentinel"));
}) as unknown as typeof fetch;

const linear = createLinearBackend({});
const ref = { id: "00000000-0000-0000-0000-000000000000", key: "HOME-1" };
const mode = process.argv[2];
const fail = (msg: string): never => {
	console.error(`FAIL ${msg}`);
	process.exit(1);
};
const expectReject = async (p: Promise<unknown>, label: string) => {
	try {
		await p;
	} catch {
		return;
	}
	fail(`expected rejection from ${label}, resolved`);
};

if (!apiKey()) fail("apiKey not read from fixture HOME");
if (mode === "unfrozen") {
	await expectReject(linear.appendEvidence(ref, "audit", "body", {}), "appendEvidence");
	if (mutationCalls !== 1) fail(`expected 1 mutation fetch, got ${mutationCalls}`);
} else if (mode === "frozen") {
	await expectReject(linear.appendEvidence(ref, "audit", "body", {}), "appendEvidence");
	await expectReject(linear.createIssue({ title: "x" }), "createIssue");
	await expectReject(linear.setNowRemote(ref), "setNowRemote");
	if (mutationCalls !== 0) fail(`expected 0 mutation fetches while frozen, got ${mutationCalls}`);
	await expectReject(linear.issueDetail("HOME-1"), "issueDetail"); // reads ignore the fence
} else {
	fail(`unknown mode ${mode}`);
}
console.log(`OK ${mode}`);
