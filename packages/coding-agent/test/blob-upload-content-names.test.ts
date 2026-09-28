import { describe, expect, it } from "bun:test";
import { LocalBlobBackend, sha256Hex } from "../src/blob-broker/broker";
import type { BlobBrokerWorkerConfig } from "../src/blob-broker/protocol";

interface CapturedRequest {
	readonly url: URL;
	readonly method: string;
	readonly headers: Headers;
	readonly body?: unknown;
}

const s3Config: BlobBrokerWorkerConfig = {
	kind: "amazon-s3",
	options: {
		bucket: "media-bucket",
		region: "us-east-1",
	},
	credentials: {
		accessKeyId: "AKIAIOSFODNN7EXAMPLE",
		secretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
	},
	bindHost: "127.0.0.1",
};

type FetchImplementation = (
	input: string | URL | Request,
	init?: RequestInit | BunFetchRequestInit,
) => Promise<Response>;

function injectedFetch(implementation: FetchImplementation): typeof globalThis.fetch {
	return Object.assign(implementation, { preconnect: globalThis.fetch.preconnect });
}

describe("LocalBlobBackend content-hashed upload names (E0119)", () => {
	it("uploads two distinct PNGs with distinct object keys matching their SHA-256 digests and never upload.png", async () => {
		const requests: CapturedRequest[] = [];
		const fakeFetch = injectedFetch(async (input, init) => {
			const inputRequest = input instanceof Request ? input : undefined;
			requests.push({
				url: new URL(inputRequest?.url ?? input.toString()),
				method: init?.method ?? inputRequest?.method ?? "GET",
				headers: new Headers(init?.headers ?? inputRequest?.headers),
				body: init?.body,
			});
			return new Response(null, { status: 200 });
		});

		const backend = new LocalBlobBackend(s3Config, fakeFetch);
		const png1 = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);
		const png2 = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 4, 5, 6]);

		const hash1 = sha256Hex(png1);
		const hash2 = sha256Hex(png2);
		expect(hash1).toMatch(/^[0-9a-f]{64}$/);
		expect(hash2).toMatch(/^[0-9a-f]{64}$/);
		expect(hash1).not.toBe(hash2);

		const pub1 = await backend.ensureBlob("key-distinct-1", "image/png", () => png1);
		const pub2 = await backend.ensureBlob("key-distinct-2", "image/png", () => png2);

		expect(pub1).not.toBeNull();
		expect(pub2).not.toBeNull();

		// Two distinct uploads captured
		expect(requests).toHaveLength(2);
		expect(requests[0].method).toBe("PUT");
		expect(requests[1].method).toBe("PUT");

		// Object keys equal <64 hex>.png matching the SHA-256 of their bytes
		const expectedKey1 = `${hash1}.png`;
		const expectedKey2 = `${hash2}.png`;
		expect(requests[0].url.pathname).toBe(`/${expectedKey1}`);
		expect(requests[1].url.pathname).toBe(`/${expectedKey2}`);
		expect(pub1?.remoteId).toBe(expectedKey1);
		expect(pub2?.remoteId).toBe(expectedKey2);
		expect(pub1?.remoteId).not.toBe(pub2?.remoteId);

		// No request path ends in upload.png
		for (const req of requests) {
			expect(req.url.pathname.endsWith("upload.png")).toBe(false);
			expect(req.url.pathname).not.toContain("upload.png");
		}
	});

	it("yields the same object name when uploading identical bytes under two different keys", async () => {
		const requests: CapturedRequest[] = [];
		const fakeFetch = injectedFetch(async (input, init) => {
			const inputRequest = input instanceof Request ? input : undefined;
			requests.push({
				url: new URL(inputRequest?.url ?? input.toString()),
				method: init?.method ?? inputRequest?.method ?? "GET",
				headers: new Headers(init?.headers ?? inputRequest?.headers),
				body: init?.body,
			});
			return new Response(null, { status: 200 });
		});

		const backend = new LocalBlobBackend(s3Config, fakeFetch);
		const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 99, 98, 97]);
		const expectedHash = sha256Hex(png);
		const expectedObjectName = `${expectedHash}.png`;

		const pubA = await backend.ensureBlob("caller-key-alpha", "image/png", () => png);
		const pubB = await backend.ensureBlob("caller-key-beta", "image/png", () => png);

		expect(pubA).not.toBeNull();
		expect(pubB).not.toBeNull();

		// Both publications yield the identical object name
		expect(pubA?.remoteId).toBe(expectedObjectName);
		expect(pubB?.remoteId).toBe(expectedObjectName);
		expect(pubA?.remoteId).toBe(pubB?.remoteId);

		// Neither request path ends in upload.png
		for (const req of requests) {
			expect(req.url.pathname).toBe(`/${expectedObjectName}`);
			expect(req.url.pathname.endsWith("upload.png")).toBe(false);
		}
	});
});
