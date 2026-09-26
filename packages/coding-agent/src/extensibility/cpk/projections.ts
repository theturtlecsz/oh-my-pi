/**
 * CPK-5 replayable disposable projections.
 *
 * Projections build useful read-only views (WebUI, Fleet Manager, Agent Hub,
 * Advisor/Observer analytics) from committed native facts without becoming
 * state authorities. Projections:
 * - Consume committed native facts from authoritative JSONL/DB outboxes.
 * - Maintain only derived, disposable checkpoints and state.
 * - Bind every projection row to source identity, version, sequence, and
 *   projection implementation version.
 * - Detect gaps, duplicate delivery, schema incompatibility, and stale cursors.
 * - Support clean rebuilds and loss recovery from native truth.
 * - Keep consumers offline-tolerant and non-blocking to authoritative commits.
 */

/** Schema discriminator for CPK-5 native facts and projection records. */
export const CPK5_SCHEMA = "cpk5/v1";

export type CpkProjectionStatus = "current" | "stale" | "offline" | "rebuilding";

export interface CpkNativeFact<T = unknown> {
	schema: typeof CPK5_SCHEMA;
	/** Identity of the native committing authority (e.g. workservice, session). */
	sourceIdentity: string;
	/** Version of the source authority schema. */
	sourceVersion: string;
	/** 1-based monotonic sequence number within this source. */
	sequence: number;
	/** Canonical event type of the committed fact. */
	factType: string;
	/** ISO 8601 timestamp of commitment. */
	timestamp: string;
	/** Immutable payload. */
	payload: T;
}

export interface CpkProjectionRow<T = unknown> {
	id: string;
	sourceIdentity: string;
	sourceVersion: string;
	sourceSequence: number;
	projectionVersion: string;
	updatedAt: string;
	data: T;
}

export interface CpkProjectionCheckpoint {
	projectionName: string;
	projectionVersion: string;
	lastSequence: number;
	lastSourceIdentity: string;
	lastSourceVersion: string;
	status: CpkProjectionStatus;
	factCount: number;
	digest: string;
}

export type CpkProjectionErrorCode =
	| "not_object"
	| "invalid_schema"
	| "invalid_fact"
	| "schema_incompatible"
	| "stale_cursor"
	| "gap_detected"
	| "projection_offline"
	| "mutation_forbidden";

export class CpkProjectionError extends Error {
	readonly code: CpkProjectionErrorCode;

	constructor(code: CpkProjectionErrorCode, message: string) {
		super(message);
		this.name = "CpkProjectionError";
		this.code = code;
	}
}

function requireNonEmptyString(value: unknown, field: string): string {
	if (typeof value !== "string" || value.length === 0) {
		throw new CpkProjectionError("invalid_fact", `fact field "${field}" must be a non-empty string`);
	}
	return value;
}

function requirePositiveInteger(value: unknown, field: string): number {
	if (typeof value !== "number" || !Number.isInteger(value) || value <= 0) {
		throw new CpkProjectionError("invalid_fact", `fact field "${field}" must be a positive integer`);
	}
	return value;
}

/**
 * Validate and canonicalize an untrusted native fact.
 * Throws {@link CpkProjectionError} on malformed input.
 */
export function parseCpkNativeFact(input: unknown): CpkNativeFact {
	if (typeof input !== "object" || input === null || Array.isArray(input)) {
		throw new CpkProjectionError("not_object", "native fact must be a JSON object");
	}
	const record = input as Record<string, unknown>;
	if (record.schema !== CPK5_SCHEMA) {
		throw new CpkProjectionError("invalid_schema", `native fact schema must be "${CPK5_SCHEMA}"`);
	}

	return {
		schema: CPK5_SCHEMA,
		sourceIdentity: requireNonEmptyString(record.sourceIdentity, "sourceIdentity"),
		sourceVersion: requireNonEmptyString(record.sourceVersion, "sourceVersion"),
		sequence: requirePositiveInteger(record.sequence, "sequence"),
		factType: requireNonEmptyString(record.factType, "factType"),
		timestamp: requireNonEmptyString(record.timestamp, "timestamp"),
		payload: record.payload,
	};
}

/**
 * Authoritative native outbox representing committed truth in JSONL or DB outbox.
 * Projections subscribe to or replay from this outbox.
 * Commits to this outbox succeed regardless of whether projections are online or offline.
 */
export class CpkNativeOutbox {
	readonly #sourceIdentity: string;
	readonly #sourceVersion: string;
	readonly #sink?: (fact: CpkNativeFact) => void;
	readonly #facts: CpkNativeFact[] = [];
	#nextSequence = 1;

	constructor(sourceIdentity = "workservice", sourceVersion = "1.0.0", sink?: (fact: CpkNativeFact) => void) {
		this.#sourceIdentity = sourceIdentity;
		this.#sourceVersion = sourceVersion;
		this.#sink = sink;
	}

	get sourceIdentity(): string {
		return this.#sourceIdentity;
	}

	get sourceVersion(): string {
		return this.#sourceVersion;
	}

	get count(): number {
		return this.#facts.length;
	}

	/**
	 * Append a committed fact to the native outbox.
	 * This is an authoritative commit; it never fails due to projection states.
	 * When a native sink is attached, writes through directly to the underlying store.
	 */
	commit<T = unknown>(factType: string, payload: T, timestamp = new Date().toISOString()): CpkNativeFact<T> {
		const fact: CpkNativeFact<T> = {
			schema: CPK5_SCHEMA,
			sourceIdentity: this.#sourceIdentity,
			sourceVersion: this.#sourceVersion,
			sequence: this.#nextSequence,
			factType,
			timestamp,
			payload,
		};
		this.#nextSequence += 1;
		this.#facts.push(fact as CpkNativeFact<unknown>);
		this.#sink?.(fact as CpkNativeFact<unknown>);
		return fact;
	}

	/** Get all committed facts as an immutable snapshot. */
	facts(): readonly CpkNativeFact[] {
		return [...this.#facts];
	}

	/** Slice facts starting from a specific sequence number. */
	slice(fromSequence: number): CpkNativeFact[] {
		return this.#facts.filter(f => f.sequence >= fromSequence);
	}

	/** Export facts as newline-delimited JSON (JSONL). */
	exportJsonl(): string {
		return this.#facts.map(fact => JSON.stringify(fact)).join("\n");
	}

	/** Import facts from a JSONL string. */
	static importJsonl(jsonl: string, sourceIdentity = "workservice", sourceVersion = "1.0.0"): CpkNativeOutbox {
		const outbox = new CpkNativeOutbox(sourceIdentity, sourceVersion);
		if (!jsonl.trim()) return outbox;

		const lines = jsonl.split("\n").filter(line => line.trim().length > 0);
		for (const line of lines) {
			const parsed = parseCpkNativeFact(JSON.parse(line));
			outbox.#facts.push(parsed);
			if (parsed.sequence >= outbox.#nextSequence) {
				outbox.#nextSequence = parsed.sequence + 1;
			}
		}
		return outbox;
	}

	/**
	 * Derive an authoritative outbox directly from native session entries (JSONL records).
	 * Eliminates secondary state ledgers by reading directly from native session persistence.
	 */
	static fromSessionEntries(
		entries: readonly unknown[],
		sourceIdentity = "session",
		sourceVersion = "3.0.0",
	): CpkNativeOutbox {
		const outbox = new CpkNativeOutbox(sourceIdentity, sourceVersion);
		let seq = 1;
		for (const raw of entries) {
			if (!raw || typeof raw !== "object") continue;
			const entry = raw as Record<string, unknown>;
			const factType =
				typeof entry.customType === "string" && entry.customType.length > 0
					? entry.customType
					: typeof entry.type === "string" && entry.type.length > 0
						? entry.type
						: "session_entry";
			const timestamp =
				typeof entry.timestamp === "string"
					? entry.timestamp
					: typeof entry.updatedAt === "string"
						? entry.updatedAt
						: new Date().toISOString();
			const payload = entry.data !== undefined ? entry.data : entry;

			outbox.#facts.push({
				schema: CPK5_SCHEMA,
				sourceIdentity,
				sourceVersion,
				sequence: seq++,
				factType,
				timestamp,
				payload,
			});
		}
		outbox.#nextSequence = seq;
		return outbox;
	}

	/**
	 * Derive an authoritative outbox directly from native session JSONL text.
	 */
	static fromSessionJsonl(jsonl: string, sourceIdentity = "session", sourceVersion = "3.0.0"): CpkNativeOutbox {
		if (!jsonl.trim()) return new CpkNativeOutbox(sourceIdentity, sourceVersion);
		const lines = jsonl.split("\n").filter(line => line.trim().length > 0);
		const entries = lines.map(l => JSON.parse(l));
		return CpkNativeOutbox.fromSessionEntries(entries, sourceIdentity, sourceVersion);
	}

	/**
	 * Derive an authoritative outbox directly from workflow/execute outbox records.
	 */
	static fromExecuteOutbox(
		records: readonly unknown[],
		sourceIdentity = "workservice",
		sourceVersion = "1.0.0",
	): CpkNativeOutbox {
		const outbox = new CpkNativeOutbox(sourceIdentity, sourceVersion);
		let seq = 1;
		for (const raw of records) {
			if (!raw || typeof raw !== "object") continue;
			const rec = raw as Record<string, unknown>;
			const factType =
				typeof rec.action === "string"
					? rec.action
					: typeof rec.customType === "string"
						? rec.customType
						: "execute_outbox";
			const timestamp =
				typeof rec.timestamp === "string"
					? rec.timestamp
					: typeof rec.at === "string"
						? rec.at
						: new Date().toISOString();
			outbox.#facts.push({
				schema: CPK5_SCHEMA,
				sourceIdentity,
				sourceVersion,
				sequence: seq++,
				factType,
				timestamp,
				payload: rec.payload ?? rec.data ?? rec,
			});
		}
		outbox.#nextSequence = seq;
		return outbox;
	}
}

export interface CpkFactIngestResult {
	accepted: boolean;
	deduplicated: boolean;
	gap: boolean;
	schemaIncompatible: boolean;
	offline: boolean;
	message: string;
}

/**
 * Base disposable projection engine.
 *
 * Holds derived rows bound to native provenance and projection version.
 * Can be disposed, wiped, and replayed from native truth at any time.
 */
export class CpkDisposableProjection<TData = unknown> {
	readonly #name: string;
	readonly #version: string;
	readonly #rows = new Map<string, CpkProjectionRow<TData>>();
	readonly #applyFact: (state: Map<string, CpkProjectionRow<TData>>, fact: CpkNativeFact) => void;
	#lastSequence = 0;
	#lastSourceIdentity = "";
	#lastSourceVersion = "";
	#factCount = 0;
	#offline = false;
	#stale = false;
	#schemaCorrupted = false;
	#rebuilding = false;

	constructor(
		name: string,
		version: string,
		applyFact: (state: Map<string, CpkProjectionRow<TData>>, fact: CpkNativeFact) => void,
	) {
		this.#name = name;
		this.#version = version;
		this.#applyFact = applyFact;
	}

	get name(): string {
		return this.#name;
	}

	get version(): string {
		return this.#version;
	}

	get rowCount(): number {
		return this.#rows.size;
	}

	get isOffline(): boolean {
		return this.#offline;
	}

	get isStale(): boolean {
		return this.#stale || this.#schemaCorrupted;
	}

	get isCurrent(): boolean {
		return !this.#offline && !this.#stale && !this.#schemaCorrupted && !this.#rebuilding;
	}

	get status(): CpkProjectionStatus {
		if (this.#rebuilding) return "rebuilding";
		if (this.#offline) return "offline";
		if (this.#stale || this.#schemaCorrupted) return "stale";
		return "current";
	}

	get rows(): readonly CpkProjectionRow<TData>[] {
		return Array.from(this.#rows.values()).sort((a, b) => a.id.localeCompare(b.id));
	}

	getRow(id: string): CpkProjectionRow<TData> | undefined {
		return this.#rows.get(id);
	}

	setOffline(offline: boolean): void {
		this.#offline = offline;
	}

	/**
	 * Compute canonical SHA-256 digest of current projection state.
	 * Incorporates full provenance (sourceIdentity, sourceVersion, sourceSequence, projectionVersion)
	 * for strict canonical parity.
	 */
	digest(): string {
		const hasher = new Bun.CryptoHasher("sha256");
		const lines = [
			`projection\t${this.#name}\t${this.#version}`,
			`sequence\t${this.#lastSequence}`,
			`status\t${this.status}`,
		];

		const sortedRows = Array.from(this.#rows.values()).sort((a, b) => a.id.localeCompare(b.id));
		for (const row of sortedRows) {
			lines.push(
				`row\t${row.id}\t${row.sourceIdentity}\t${row.sourceVersion}\t${row.sourceSequence}\t${row.projectionVersion}\t${JSON.stringify(row.data)}`,
			);
		}

		hasher.update(lines.join("\n"));
		return `sha256:${hasher.digest("hex")}`;
	}

	get checkpoint(): CpkProjectionCheckpoint {
		return {
			projectionName: this.#name,
			projectionVersion: this.#version,
			lastSequence: this.#lastSequence,
			lastSourceIdentity: this.#lastSourceIdentity,
			lastSourceVersion: this.#lastSourceVersion,
			status: this.status,
			factCount: this.#factCount,
			digest: this.digest(),
		};
	}

	/**
	 * Restore a previously saved checkpoint.
	 */
	restoreCheckpoint(checkpoint: CpkProjectionCheckpoint): void {
		this.dispose();
		this.#lastSequence = checkpoint.lastSequence;
		this.#lastSourceIdentity = checkpoint.lastSourceIdentity;
		this.#lastSourceVersion = checkpoint.lastSourceVersion;
		this.#factCount = checkpoint.factCount;
		this.#stale = checkpoint.status === "stale";
	}

	/**
	 * Verify projection cursor against an authoritative native outbox.
	 */
	verifyCursor(outbox: CpkNativeOutbox): { valid: boolean; cursor: number; outboxCount: number; isStale: boolean } {
		const outboxCount = outbox.count;
		const isStale = this.#lastSequence > outboxCount || this.#stale || this.#schemaCorrupted;
		return {
			valid: !isStale && this.#lastSequence <= outboxCount,
			cursor: this.#lastSequence,
			outboxCount,
			isStale,
		};
	}

	/**
	 * Ingest a committed fact from native outbox.
	 * Detects duplicates, gaps, and schema incompatibility.
	 * Schema incompatibility sticks until an explicit corruption recovery/rebuild is performed.
	 */
	consume(fact: CpkNativeFact): CpkFactIngestResult {
		if (this.#offline) {
			return {
				accepted: false,
				deduplicated: false,
				gap: false,
				schemaIncompatible: false,
				offline: true,
				message: `projection "${this.#name}" is offline; fact #${fact.sequence} not ingested`,
			};
		}

		if (this.#schemaCorrupted) {
			this.#stale = true;
			return {
				accepted: false,
				deduplicated: false,
				gap: false,
				schemaIncompatible: true,
				offline: false,
				message: `projection "${this.#name}" is corrupted by previous incompatible schema; rebuild required`,
			};
		}

		const factSchema: string = fact.schema;
		if (factSchema !== CPK5_SCHEMA) {
			this.#stale = true;
			this.#schemaCorrupted = true;
			return {
				accepted: false,
				deduplicated: false,
				gap: false,
				schemaIncompatible: true,
				offline: false,
				message: `incompatible schema "${factSchema}"; expected "${CPK5_SCHEMA}"`,
			};
		}

		// Duplicate delivery detection
		if (fact.sequence <= this.#lastSequence) {
			return {
				accepted: false,
				deduplicated: true,
				gap: false,
				schemaIncompatible: false,
				offline: false,
				message: `duplicate fact #${fact.sequence} detected and ignored (cursor at #${this.#lastSequence})`,
			};
		}

		// Gap detection
		if (fact.sequence > this.#lastSequence + 1) {
			this.#stale = true;
			return {
				accepted: false,
				deduplicated: false,
				gap: true,
				schemaIncompatible: false,
				offline: false,
				message: `gap detected: expected sequence #${this.#lastSequence + 1}, received #${fact.sequence}`,
			};
		}

		// Ingest fact
		this.#applyFact(this.#rows, fact);
		this.#lastSequence = fact.sequence;
		this.#lastSourceIdentity = fact.sourceIdentity;
		this.#lastSourceVersion = fact.sourceVersion;
		this.#factCount += 1;
		this.#stale = false;

		return {
			accepted: true,
			deduplicated: false,
			gap: false,
			schemaIncompatible: false,
			offline: false,
			message: `fact #${fact.sequence} (${fact.factType}) ingested successfully`,
		};
	}

	/**
	 * Completely dispose derived projection state.
	 * Proves that projection state is disposable and not authoritative.
	 */
	dispose(): void {
		this.#rows.clear();
		this.#lastSequence = 0;
		this.#lastSourceIdentity = "";
		this.#lastSourceVersion = "";
		this.#factCount = 0;
		this.#stale = false;
		this.#schemaCorrupted = false;
	}

	/**
	 * Replay and rebuild projection strictly from authoritative native facts.
	 */
	rebuild(facts: readonly CpkNativeFact[]): void {
		this.#rebuilding = true;
		this.dispose();

		const sortedFacts = [...facts].sort((a, b) => a.sequence - b.sequence);
		for (const fact of sortedFacts) {
			this.consume(fact);
		}
		this.#rebuilding = false;
	}

	/**
	 * Recover after checkpoint loss by replaying native facts.
	 */
	recoverFromCheckpointLoss(facts: readonly CpkNativeFact[]): void {
		this.rebuild(facts);
	}

	/**
	 * Recover after schema corruption or incompatibility by resetting corruption state and replaying.
	 */
	recoverFromCorruption(facts: readonly CpkNativeFact[]): void {
		this.#schemaCorrupted = false;
		this.#stale = false;
		this.rebuild(facts);
	}
}

/**
 * Thin WebUI Consumer Projection.
 *
 * Consumes task and session native facts into a thin, read-only UI view:
 * - task list and status
 * - step counts
 * - visibility banner that explicitly flags stale, offline, or rebuilding status
 * - zero mutation authority: cannot write, modify, or commit native state.
 */
export interface WebUiTaskData {
	taskId: string;
	title: string;
	status: "pending" | "running" | "completed" | "failed";
	completedSteps: number;
	lastSummary?: string;
}

export interface WebUiBanner {
	status: CpkProjectionStatus;
	isCurrent: boolean;
	message: string;
}

export class CpkWebUiProjection {
	readonly #projection: CpkDisposableProjection<WebUiTaskData>;

	constructor(version = "1.0.0") {
		this.#projection = new CpkDisposableProjection<WebUiTaskData>("webui-view", version, (state, fact) => {
			const payload = fact.payload as Record<string, unknown>;
			if (fact.factType === "task_created") {
				const taskId = String(payload.taskId ?? `task-${fact.sequence}`);
				const title = String(payload.title ?? "Untitled Task");
				state.set(taskId, {
					id: taskId,
					sourceIdentity: fact.sourceIdentity,
					sourceVersion: fact.sourceVersion,
					sourceSequence: fact.sequence,
					projectionVersion: version,
					updatedAt: fact.timestamp,
					data: {
						taskId,
						title,
						status: "pending",
						completedSteps: 0,
					},
				});
			} else if (fact.factType === "task_updated") {
				const taskId = String(payload.taskId);
				const existing = state.get(taskId);
				if (existing) {
					state.set(taskId, {
						...existing,
						sourceSequence: fact.sequence,
						updatedAt: fact.timestamp,
						data: {
							...existing.data,
							status: (payload.status as WebUiTaskData["status"] | undefined) ?? existing.data.status,
							lastSummary: payload.summary !== undefined ? String(payload.summary) : existing.data.lastSummary,
						},
					});
				}
			} else if (fact.factType === "step_completed") {
				const taskId = String(payload.taskId);
				const existing = state.get(taskId);
				if (existing) {
					state.set(taskId, {
						...existing,
						sourceSequence: fact.sequence,
						updatedAt: fact.timestamp,
						data: {
							...existing.data,
							completedSteps: existing.data.completedSteps + 1,
						},
					});
				}
			}
		});
	}

	get version(): string {
		return this.#projection.version;
	}

	get status(): CpkProjectionStatus {
		return this.#projection.status;
	}

	get isCurrent(): boolean {
		return this.#projection.isCurrent;
	}

	get tasks(): readonly WebUiTaskData[] {
		return this.#projection.rows.map(r => r.data);
	}

	get activeTasksCount(): number {
		return this.tasks.filter(t => t.status === "running" || t.status === "pending").length;
	}

	get banner(): WebUiBanner {
		if (this.#projection.status === "rebuilding") {
			return {
				status: "rebuilding",
				isCurrent: false,
				message: "WebUI projection is rebuilding from native committed facts.",
			};
		}
		if (this.#projection.isOffline) {
			return {
				status: "offline",
				isCurrent: false,
				message: "WebUI is offline; showing cached snapshot. Commits continue in native outbox.",
			};
		}
		if (this.#projection.isStale) {
			return {
				status: "stale",
				isCurrent: false,
				message: "WebUI projection is stale due to missing or out-of-order facts. Rebuilding required.",
			};
		}
		return {
			status: "current",
			isCurrent: true,
			message: "WebUI projection is synchronized with native committed facts.",
		};
	}

	digest(): string {
		return this.#projection.digest();
	}

	setOffline(offline: boolean): void {
		this.#projection.setOffline(offline);
	}

	consume(fact: CpkNativeFact): CpkFactIngestResult {
		return this.#projection.consume(fact);
	}

	rebuild(facts: readonly CpkNativeFact[]): void {
		this.#projection.rebuild(facts);
	}

	dispose(): void {
		this.#projection.dispose();
	}

	/**
	 * Thin consumers have zero mutation authority; mutations must go to native authorities.
	 */
	createTask(_task: unknown): never {
		throw new CpkProjectionError(
			"mutation_forbidden",
			"projections are read-only thin views; state mutations must be committed through native authorities",
		);
	}

	updateTask(_taskId: string, _update: unknown): never {
		throw new CpkProjectionError(
			"mutation_forbidden",
			"projections are read-only thin views; state mutations must be committed through native authorities",
		);
	}

	deleteTask(_taskId: string): never {
		throw new CpkProjectionError(
			"mutation_forbidden",
			"projections are read-only thin views; state mutations must be committed through native authorities",
		);
	}

	/**
	 * Construct a thin WebUI projection directly from native session entries.
	 */
	static fromSessionEntries(entries: readonly unknown[], version = "1.0.0"): CpkWebUiProjection {
		const outbox = CpkNativeOutbox.fromSessionEntries(entries);
		const proj = new CpkWebUiProjection(version);
		proj.rebuild(outbox.facts());
		return proj;
	}

	/**
	 * Construct a thin WebUI projection directly from session JSONL text.
	 */
	static fromSessionJsonl(jsonl: string, version = "1.0.0"): CpkWebUiProjection {
		const outbox = CpkNativeOutbox.fromSessionJsonl(jsonl);
		const proj = new CpkWebUiProjection(version);
		proj.rebuild(outbox.facts());
		return proj;
	}
}

/**
 * Thin Fleet Manager Consumer Projection.
 *
 * Consumes worker and assignment native facts into a thin, read-only fleet overview:
 * - registered workers and statuses
 * - active assignments
 * - fleet capacity and load
 * - visibility banner that explicitly flags stale, offline, or rebuilding status
 * - zero scheduling authority: cannot dispatch, reallocate, or commit native state.
 */
export interface FleetWorkerData {
	workerId: string;
	role: string;
	status: "idle" | "busy" | "stopped";
	currentTask?: string;
	lastHeartbeat: string;
}

export interface FleetBanner {
	status: CpkProjectionStatus;
	isCurrent: boolean;
	message: string;
}

export class CpkFleetManagerProjection {
	readonly #projection: CpkDisposableProjection<FleetWorkerData>;

	constructor(version = "1.0.0") {
		this.#projection = new CpkDisposableProjection<FleetWorkerData>("fleet-manager-view", version, (state, fact) => {
			const payload = fact.payload as Record<string, unknown>;
			if (fact.factType === "worker_registered") {
				const workerId = String(payload.workerId ?? `worker-${fact.sequence}`);
				const role = String(payload.role ?? "general");
				state.set(workerId, {
					id: workerId,
					sourceIdentity: fact.sourceIdentity,
					sourceVersion: fact.sourceVersion,
					sourceSequence: fact.sequence,
					projectionVersion: version,
					updatedAt: fact.timestamp,
					data: {
						workerId,
						role,
						status: "idle",
						lastHeartbeat: fact.timestamp,
					},
				});
			} else if (fact.factType === "worker_heartbeat") {
				const workerId = String(payload.workerId);
				const existing = state.get(workerId);
				if (existing) {
					state.set(workerId, {
						...existing,
						sourceSequence: fact.sequence,
						updatedAt: fact.timestamp,
						data: {
							...existing.data,
							lastHeartbeat: fact.timestamp,
						},
					});
				}
			} else if (fact.factType === "task_assigned") {
				const workerId = String(payload.workerId);
				const taskId = String(payload.taskId);
				const existing = state.get(workerId);
				if (existing) {
					state.set(workerId, {
						...existing,
						sourceSequence: fact.sequence,
						updatedAt: fact.timestamp,
						data: {
							...existing.data,
							status: "busy",
							currentTask: taskId,
						},
					});
				}
			} else if (fact.factType === "worker_stopped") {
				const workerId = String(payload.workerId);
				const existing = state.get(workerId);
				if (existing) {
					state.set(workerId, {
						...existing,
						sourceSequence: fact.sequence,
						updatedAt: fact.timestamp,
						data: {
							...existing.data,
							status: "stopped",
							currentTask: undefined,
						},
					});
				}
			}
		});
	}

	get version(): string {
		return this.#projection.version;
	}

	get status(): CpkProjectionStatus {
		return this.#projection.status;
	}

	get isCurrent(): boolean {
		return this.#projection.isCurrent;
	}

	get workers(): readonly FleetWorkerData[] {
		return this.#projection.rows.map(r => r.data);
	}

	get fleetCapacity(): number {
		return this.workers.filter(w => w.status !== "stopped").length;
	}

	get busyWorkersCount(): number {
		return this.workers.filter(w => w.status === "busy").length;
	}

	get banner(): FleetBanner {
		if (this.#projection.status === "rebuilding") {
			return {
				status: "rebuilding",
				isCurrent: false,
				message: "Fleet Manager view is rebuilding from native committed facts.",
			};
		}
		if (this.#projection.isOffline) {
			return {
				status: "offline",
				isCurrent: false,
				message: "Fleet Manager view is offline; dispatch proceeds through native authority.",
			};
		}
		if (this.#projection.isStale) {
			return {
				status: "stale",
				isCurrent: false,
				message: "Fleet Manager view is stale; cursor requires replay from native outbox.",
			};
		}
		return {
			status: "current",
			isCurrent: true,
			message: "Fleet Manager view is synchronized with native committed facts.",
		};
	}

	digest(): string {
		return this.#projection.digest();
	}

	setOffline(offline: boolean): void {
		this.#projection.setOffline(offline);
	}

	consume(fact: CpkNativeFact): CpkFactIngestResult {
		return this.#projection.consume(fact);
	}

	rebuild(facts: readonly CpkNativeFact[]): void {
		this.#projection.rebuild(facts);
	}

	dispose(): void {
		this.#projection.dispose();
	}

	/**
	 * Thin consumers have zero scheduling/mutation authority.
	 */
	registerWorker(_worker: unknown): never {
		throw new CpkProjectionError(
			"mutation_forbidden",
			"projections are read-only thin views; state mutations must be committed through native authorities",
		);
	}

	dispatchWorker(_workerId: string, _taskId: string): never {
		throw new CpkProjectionError(
			"mutation_forbidden",
			"projections are read-only thin views; state mutations must be committed through native authorities",
		);
	}

	stopWorker(_workerId: string): never {
		throw new CpkProjectionError(
			"mutation_forbidden",
			"projections are read-only thin views; state mutations must be committed through native authorities",
		);
	}

	/**
	 * Construct a thin Fleet Manager projection directly from native session entries.
	 */
	static fromSessionEntries(entries: readonly unknown[], version = "1.0.0"): CpkFleetManagerProjection {
		const outbox = CpkNativeOutbox.fromSessionEntries(entries);
		const proj = new CpkFleetManagerProjection(version);
		proj.rebuild(outbox.facts());
		return proj;
	}
}

/**
 * Compare two projections for canonical parity before promotion.
 * Verifies both canonical state digest and strict provenance parity across all rows.
 */
export function compareProjectionParity<T>(
	candidate: CpkDisposableProjection<T>,
	baseline: CpkDisposableProjection<T>,
): {
	match: boolean;
	candidateDigest: string;
	baselineDigest: string;
	candidateRows: number;
	baselineRows: number;
	provenanceMatch: boolean;
} {
	const candidateDigest = candidate.digest();
	const baselineDigest = baseline.digest();
	const candidateRows = candidate.rows;
	const baselineRows = baseline.rows;

	let provenanceMatch = candidateRows.length === baselineRows.length;
	if (provenanceMatch) {
		const baselineIterator = baselineRows[Symbol.iterator]();
		for (const c of candidateRows) {
			const nextBaseline = baselineIterator.next();
			if (nextBaseline.done) {
				provenanceMatch = false;
				break;
			}
			const b = nextBaseline.value;
			if (
				c.sourceIdentity !== b.sourceIdentity ||
				c.sourceVersion !== b.sourceVersion ||
				c.projectionVersion !== b.projectionVersion
			) {
				provenanceMatch = false;
				break;
			}
		}
	}

	const match = candidateDigest === baselineDigest && provenanceMatch;
	return {
		match,
		candidateDigest,
		baselineDigest,
		candidateRows: candidate.rowCount,
		baselineRows: baseline.rowCount,
		provenanceMatch,
	};
}
