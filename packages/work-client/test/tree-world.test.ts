import { expect, test } from "bun:test";
import { WorkClient } from "../src/index";

const WORKSPACE_ID = "00000000-0000-0000-0000-000000000001";

const EMPTY_TREE = {
	workspace_id: WORKSPACE_ID,
	items: [],
	relations: [],
	projects: [],
};

function recordingClient(urls: string[]): WorkClient {
	return new WorkClient(
		"http://127.0.0.1:54322",
		WORKSPACE_ID,
		() => "token",
		async (input, init) => {
			urls.push(new Request(String(input), init).url);
			return Response.json(EMPTY_TREE);
		},
	);
}

test("tree() requests the path with no query string", async () => {
	const urls: string[] = [];
	await recordingClient(urls).tree();
	expect(urls).toEqual([`http://127.0.0.1:54322/v1/workspaces/${WORKSPACE_ID}/tree`]);
});

test('tree({ world: "media-discovery" }) requests ?world=media-discovery', async () => {
	const urls: string[] = [];
	await recordingClient(urls).tree({ world: "media-discovery" });
	expect(urls).toEqual([`http://127.0.0.1:54322/v1/workspaces/${WORKSPACE_ID}/tree?world=media-discovery`]);
});
