import { afterEach, beforeEach, describe, expect, it, spyOn, vi } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { FetchImpl } from "@oh-my-pi/pi-ai/types";
import { renderProviderModels } from "@oh-my-pi/pi-coding-agent/cli/models-cli";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import { removeSyncWithRetries, Snowflake } from "@oh-my-pi/pi-utils";

const PROVIDER = "offerings-lab";
const LISTING_URL = "https://offerings.example.test/v1/models";

interface DiscoveryEvidence {
	provider: string;
	status: string;
	stale: boolean;
	source?: string;
	fetchedAt?: number;
}

interface ListingJson {
	models: { provider: string; id: string }[];
	discovery?: Record<string, DiscoveryEvidence>;
}

describe("omp models --json offerings evidence", () => {
	let tempDir: string;
	let authStorage: AuthStorage;
	let registry: ModelRegistry;
	let requests: { method: string; url: string }[];
	let listingFails: boolean;

	beforeEach(async () => {
		tempDir = path.join(os.tmpdir(), `pi-test-offerings-evidence-${Snowflake.next()}`);
		fs.mkdirSync(tempDir, { recursive: true });
		const modelsPath = path.join(tempDir, "models.yml");
		fs.writeFileSync(
			modelsPath,
			[
				"providers:",
				`  ${PROVIDER}:`,
				"    baseUrl: https://offerings.example.test/v1",
				"    apiKey: sk-test",
				"    api: openai-completions",
				"    auth: apiKey",
				"    discovery:",
				"      type: openai-models-list",
			].join("\n"),
		);
		authStorage = await AuthStorage.create(path.join(tempDir, "auth.db"));
		requests = [];
		listingFails = false;
		// Local GET-listing transport: any other URL or method (e.g. an inference call) throws.
		const fetchStub: FetchImpl = async (input, init) => {
			const method = init?.method ?? "GET";
			const url = String(input);
			requests.push({ method, url });
			if (method !== "GET" || url !== LISTING_URL) throw new Error(`Unexpected request: ${method} ${url}`);
			if (listingFails) return new Response("upstream down", { status: 503 });
			return new Response(JSON.stringify({ data: [{ id: "lab-model-1" }] }), {
				status: 200,
				headers: { "Content-Type": "application/json" },
			});
		};
		registry = new ModelRegistry(authStorage, modelsPath, {
			fetch: fetchStub,
			cacheDbPath: path.join(tempDir, "models.db"),
		});
	});

	afterEach(() => {
		vi.restoreAllMocks();
		authStorage.close();
		removeSyncWithRetries(tempDir);
	});

	function listJson(pattern: string): ListingJson {
		const chunks: string[] = [];
		spyOn(process.stdout, "write").mockImplementation((chunk: string | Uint8Array) => {
			chunks.push(typeof chunk === "string" ? chunk : new TextDecoder().decode(chunk));
			return true;
		});
		try {
			renderProviderModels(registry, "ls", pattern, true);
		} finally {
			vi.restoreAllMocks();
		}
		return JSON.parse(chunks.join("")) as ListingJson;
	}

	it("exposes the exact provider and real fetch time after a successful refresh", async () => {
		const before = Date.now();
		await registry.refreshProvider(PROVIDER);
		const after = Date.now();

		const json = listJson(PROVIDER);
		expect(json.models.map(m => m.id)).toContain("lab-model-1");
		const evidence = json.discovery?.[PROVIDER];
		expect(evidence?.provider).toBe(PROVIDER);
		expect(evidence?.status).toBe("ok");
		expect(evidence?.stale).toBe(false);
		expect(evidence?.fetchedAt).toBeGreaterThanOrEqual(before);
		expect(evidence?.fetchedAt).toBeLessThanOrEqual(after);
		expect(requests.length).toBeGreaterThan(0);
		for (const request of requests) expect(request).toEqual({ method: "GET", url: LISTING_URL });
	});

	it("reports stale/cache state when a later retrieval fails", async () => {
		await registry.refreshProvider(PROVIDER);
		listingFails = true;
		await registry.refreshProvider(PROVIDER);

		const evidence = listJson(PROVIDER).discovery?.[PROVIDER];
		expect(evidence?.provider).toBe(PROVIDER);
		expect(evidence?.stale).toBe(true);
		expect(evidence?.status).not.toBe("ok");
		for (const request of requests) expect(request).toEqual({ method: "GET", url: LISTING_URL });
	});

	it("gives a substring typo no exact-provider success", async () => {
		await registry.refreshProvider(PROVIDER);

		const typo = listJson("offerings");
		expect(typo.models.map(m => m.provider)).toContain(PROVIDER);
		expect(typo.discovery?.offerings).toBeUndefined();
		expect(typo.discovery?.[PROVIDER]?.status).toBe("ok");
	});
});
