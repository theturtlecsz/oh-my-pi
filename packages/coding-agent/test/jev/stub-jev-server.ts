import type { Server } from "bun";

export type StubJevMode = "ok" | "delay" | "500" | "401" | "403" | "malformed" | "off-list";

export interface RecordedRequest {
	method: string;
	url: string;
	headers: Headers;
	body: unknown;
	rawBody: string;
}

export interface StubJevServerOptions {
	mode?: StubJevMode;
	sequence?: StubJevMode[];
	port?: number;
	serverId?: string;
	delayMs?: number;
	answers?: Record<string, unknown>;
}

export class StubJevServer {
	#server: Server<unknown>;
	#mode: StubJevMode;
	#sequence: StubJevMode[] | undefined;
	#serverId: string;
	#delayMs: number;
	#answers: Record<string, unknown> | undefined;
	#requests: RecordedRequest[] = [];

	constructor(options?: StubJevServerOptions) {
		this.#mode = options?.mode ?? "ok";
		this.#sequence = options?.sequence ? [...options.sequence] : undefined;
		this.#serverId = options?.serverId ?? "stub-jev-srv-1";
		this.#delayMs = options?.delayMs ?? 4000;
		this.#answers = options?.answers;

		this.#server = Bun.serve({
			port: options?.port ?? 0,
			fetch: async (req: Request) => {
				const rawBody = await req.text();
				let parsedBody: unknown;
				try {
					parsedBody = JSON.parse(rawBody);
				} catch {
					parsedBody = undefined;
				}

				this.#requests.push({
					method: req.method,
					url: req.url,
					headers: req.headers,
					body: parsedBody,
					rawBody,
				});

				const currentMode = this.#sequence && this.#sequence.length > 0 ? this.#sequence.shift()! : this.#mode;

				if (currentMode === "delay") {
					await Bun.sleep(this.#delayMs);
				}

				if (currentMode === "500") {
					return new Response("Internal Server Error", { status: 500 });
				}

				if (currentMode === "401") {
					return new Response("Unauthorized", { status: 401 });
				}

				if (currentMode === "403") {
					return new Response("Forbidden", { status: 403 });
				}

				if (currentMode === "malformed") {
					return new Response("{not valid json", {
						status: 200,
						headers: { "Content-Type": "application/json" },
					});
				}

				if (currentMode === "off-list") {
					return Response.json({
						id: this.#serverId,
						answers: {
							primary_type: {
								probabilities: {
									off_list_unknown_type: 0.99,
								},
							},
						},
					});
				}

				if (this.#answers) {
					return Response.json({
						id: this.#serverId,
						answers: this.#answers,
					});
				}

				const questions = (parsedBody as { questions?: Record<string, unknown> } | undefined)?.questions;
				const builtAnswers: Record<string, unknown> = {};
				if (questions && typeof questions === "object") {
					for (const [name, q] of Object.entries(questions)) {
						const question = q as { type?: string; options?: string[] };
						if (question.type === "choice" && Array.isArray(question.options) && question.options.length > 0) {
							const probs: Record<string, number> = {};
							for (let i = 0; i < question.options.length; i++) {
								probs[question.options[i]] = i === 0 ? 0.8 : 0.2 / (question.options.length - 1);
							}
							builtAnswers[name] = { probabilities: probs };
						} else {
							builtAnswers[name] = { probability: 0.85 };
						}
					}
				}

				return Response.json({
					id: this.#serverId,
					answers: builtAnswers,
				});
			},
		});
	}

	get port(): number {
		return this.#server.port ?? 0;
	}

	get baseUrl(): string {
		return `http://127.0.0.1:${this.#server.port ?? 0}`;
	}

	get requests(): readonly RecordedRequest[] {
		return this.#requests;
	}

	setMode(mode: StubJevMode): void {
		this.#mode = mode;
	}

	setSequence(seq: StubJevMode[]): void {
		this.#sequence = [...seq];
	}

	setDelayMs(ms: number): void {
		this.#delayMs = ms;
	}

	setAnswers(answers: Record<string, unknown>): void {
		this.#answers = answers;
	}

	reset(): void {
		this.#requests.length = 0;
	}

	stop(): void {
		this.#server.stop(true);
	}
}

export function startStubJevServer(options?: StubJevServerOptions): StubJevServer {
	return new StubJevServer(options);
}
