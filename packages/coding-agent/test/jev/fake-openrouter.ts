import type { FetchImpl } from "@oh-my-pi/pi-utils";

/**
 * Fake OpenRouter transport for the router measurement.
 *
 * A plain injected `FetchImpl`, not a server: the router tests must not open
 * any socket, so nothing here can reach `openrouter.ai`. Requests are recorded
 * (URL, headers, body) so a test can prove the origin and the pinned model, and
 * the canned replies let a test drive the router's own label, token usage, a
 * generation record, and every transport failure mode.
 */

export type FakeRouterMode = "ok" | "500" | "401" | "403" | "malformed" | "empty-choices" | "network" | "timeout";

export interface FakeRouterReply {
	/** Text the routed model "produced". */
	text?: string;
	/** `model` field on the response (the routed model). */
	routedModel?: string;
	promptTokens?: number;
	completionTokens?: number;
	/** `id` used to look up the generation record. */
	id?: string;
}

export interface RecordedRouterRequest {
	method: string;
	url: string;
	authorization?: string;
	body: unknown;
}

export interface FakeOpenRouterOptions {
	mode?: FakeRouterMode;
	sequence?: FakeRouterMode[];
	reply?: FakeRouterReply;
	/** Reply used after {@link FakeOpenRouterOptions.sequence} is exhausted. */
	sequenceReply?: FakeRouterReply;
	/** Generation-record payload keyed by generation id. */
	generations?: Record<string, { total_cost?: number; model?: string; reasoning?: string }>;
}

export class FakeOpenRouterTransport {
	#mode: FakeRouterMode;
	#sequence: FakeRouterMode[] | undefined;
	#reply: FakeRouterReply;
	#sequenceReply: FakeRouterReply;
	#generations: Record<string, { total_cost?: number; model?: string; reasoning?: string }>;
	#requests: RecordedRouterRequest[] = [];
	#generationRequests: string[] = [];

	constructor(options?: FakeOpenRouterOptions) {
		this.#mode = options?.mode ?? "ok";
		this.#sequence = options?.sequence ? [...options.sequence] : undefined;
		this.#reply = options?.reply ?? {
			text: "medium",
			routedModel: "openai/gpt-5-mini",
			promptTokens: 120,
			completionTokens: 4,
			id: "gen-1",
		};
		this.#sequenceReply = options?.sequenceReply ?? this.#reply;
		this.#generations = options?.generations ?? {};
	}

	get requests(): readonly RecordedRouterRequest[] {
		return this.#requests;
	}

	get generationRequests(): readonly string[] {
		return this.#generationRequests;
	}

	get fetch(): FetchImpl {
		return async (input, init) => {
			const url =
				typeof input === "string" ? input : input instanceof URL ? input.toString() : (input as Request).url;
			const method = init?.method ?? "GET";
			const headers = new Headers(init?.headers);

			if (url.includes("/api/v1/generation")) {
				const id = new URL(url).searchParams.get("id") ?? "";
				this.#generationRequests.push(id);
				const record = this.#generations[id];
				if (!record) return new Response("not found", { status: 404 });
				return Response.json({ data: record });
			}

			let body: unknown;
			try {
				body = JSON.parse(typeof init?.body === "string" ? init.body : "null");
			} catch {
				body = undefined;
			}
			this.#requests.push({
				method,
				url,
				authorization: headers.get("Authorization") ?? undefined,
				body,
			});

			const current = this.#sequence && this.#sequence.length > 0 ? this.#sequence.shift()! : this.#mode;
			switch (current) {
				case "500":
					return new Response("Internal Server Error", { status: 500 });
				case "401":
					return new Response("Unauthorized", { status: 401 });
				case "403":
					return new Response("Forbidden", { status: 403 });
				case "malformed":
					return new Response("{not valid json", { status: 200, headers: { "Content-Type": "application/json" } });
				case "empty-choices":
					return Response.json({ id: "gen-none", model: "openai/gpt-5-mini", choices: [] });
				case "network":
					throw new TypeError("fetch failed");
				case "timeout": {
					const reason = new DOMException("The operation timed out.", "TimeoutError");
					const signal = init?.signal;
					if (signal) {
						signal.throwIfAborted?.();
						return await new Promise<Response>((_resolve, reject) => {
							signal.addEventListener("abort", () => reject(reason), { once: true });
						});
					}
					throw reason;
				}
				default: {
					const reply = this.#sequence ? this.#sequenceReply : this.#reply;
					return Response.json({
						id: reply.id ?? "gen-1",
						model: reply.routedModel ?? "openai/gpt-5-mini",
						choices: [{ message: { role: "assistant", content: reply.text ?? "medium" } }],
						usage: {
							prompt_tokens: reply.promptTokens ?? 0,
							completion_tokens: reply.completionTokens ?? 0,
							total_tokens: (reply.promptTokens ?? 0) + (reply.completionTokens ?? 0),
						},
					});
				}
			}
		};
	}

	setMode(mode: FakeRouterMode): void {
		this.#mode = mode;
	}

	setSequence(seq: FakeRouterMode[]): void {
		this.#sequence = [...seq];
	}

	setReply(reply: FakeRouterReply): void {
		this.#reply = reply;
	}

	setSequenceReply(reply: FakeRouterReply): void {
		this.#sequenceReply = reply;
	}

	reset(): void {
		this.#requests.length = 0;
		this.#generationRequests.length = 0;
	}
}
