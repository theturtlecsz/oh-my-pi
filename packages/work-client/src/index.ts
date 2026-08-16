export type UUID = string;
export type OperationReceipt = {
	operation_id: UUID;
	request_id: UUID;
	state: "applied" | "replayed" | "rejected" | "pending_approval";
	request_sha256: string;
	result_sha256: string;
	diagnostics: string[];
};
export type WorkItem = { work_id: UUID; revision_id: UUID; key: string; state: string };
export type CommandResult =
	| { type: "create_work_batch"; items: WorkItem[] }
	| { type: "revise_work"; revision_id: UUID; changed: boolean }
	| { type: "set_work_state" | "complete_work"; work_id: UUID; state: string; row_version: number }
	| {
			type: "put_relation" | "remove_relation";
			source_work_id: UUID;
			target_work_id: UUID;
			kind: "parent" | "blocks" | "duplicate_of" | "related";
			active: boolean;
	  }
	| { type: "set_focus" | "clear_focus"; workspace_id: UUID; owner_id: UUID; work_id: UUID | null; version: number }
	| { type: "append_evidence"; receipt: EvidenceReceipt }
	| { type: "request_closeout"; intent: CloseoutIntent }
	| { type: "record_project_health"; health: ProjectHealth };
export type CommandResponse = { receipt: OperationReceipt; result: CommandResult };
export type WorkErrorBody = {
	error: { code: string; request_id: UUID | null; correlation_id: UUID | null; diagnostics: string[] };
};
export type EvidenceReceipt = {
	receipt_id: UUID;
	work_id: UUID;
	revision_id: UUID;
	candidate_id: UUID;
	kind: "plan" | "verification" | "audit" | "push" | "closeout";
	payload_sha256: string;
	issuer: string;
	issued_at: string;
	candidate_sha256?: string | null;
	candidate_commit?: string | null;
	verdict?: "PASS" | "NEEDS_FIX" | "BLOCKED" | null;
	independent?: boolean;
	remote_ref?: string | null;
	remote_commit?: string | null;
};
export type CloseoutIntent = {
	intent_id: UUID;
	work_id: UUID;
	revision_id: UUID;
	candidate_id: UUID;
	state: "pending" | "completed";
	requested_at?: string;
};
export type ProjectHealth = {
	workspace_id: UUID;
	project_id: UUID;
	health: "onTrack" | "atRisk" | "offTrack";
	updated_at: string;
};
export type WorkItemView = Record<string, unknown>;
export type WorkflowView = Record<string, unknown>;
export type WorkspaceTree = Record<string, unknown>;
export type FocusSlot = { workspace_id: UUID; owner_id: UUID; work_id: UUID | null; version: number };
export type StoredOperation = {
	receipt: OperationReceipt;
	command_type: string;
	request_id: UUID;
	correlation_id: UUID;
	result: CommandResult | null;
};
export type CommandEnvelope = {
	api_version: "work.omp.dev/v1";
	workspace_id: UUID;
	operation_id: UUID;
	request_id: UUID;
	correlation_id: UUID;
	command: { type: string; payload: Record<string, unknown> };
};
export type Fetch = typeof fetch;

export class WorkError extends Error {
	constructor(
		readonly status: number,
		readonly body: WorkErrorBody["error"],
	) {
		super(body.code);
	}
}

export class WorkClient {
	#baseUrl: string;
	#workspaceId: UUID;
	#bearerToken: () => string;
	#request: Fetch;
	#timeoutMs: number;

	constructor(
		baseUrl: string,
		workspaceId: UUID,
		bearerToken: () => string,
		request: Fetch = fetch,
		timeoutMs = 10_000,
	) {
		this.#baseUrl = baseUrl;
		this.#workspaceId = workspaceId;
		this.#bearerToken = bearerToken;
		this.#request = request;
		this.#timeoutMs = timeoutMs;
	}

	execute(envelope: CommandEnvelope): Promise<CommandResponse> {
		return this.#call("/v1/commands", { method: "POST", body: JSON.stringify(envelope) });
	}

	workItem(key: string): Promise<WorkItemView> {
		return this.#call(`/v1/work-items/${encodeURIComponent(key)}`);
	}

	workflow(key: string): Promise<WorkflowView> {
		return this.#call(`/v1/work-items/${encodeURIComponent(key)}/workflow`);
	}

	tree(): Promise<WorkspaceTree> {
		return this.#call(`/v1/workspaces/${this.#workspaceId}/tree`);
	}

	focus(ownerId: UUID): Promise<FocusSlot> {
		return this.#call(`/v1/workspaces/${this.#workspaceId}/focus/${ownerId}`);
	}

	operation(operationId: UUID): Promise<StoredOperation> {
		return this.#call(`/v1/operations/${operationId}`);
	}

	async #call<T>(path: string, init: RequestInit = {}): Promise<T> {
		const response = await this.#request(`${this.#baseUrl}${path}`, {
			...init,
			headers: {
				Authorization: `Bearer ${this.#bearerToken()}`,
				"X-OMP-Workspace-ID": this.#workspaceId,
				"Content-Type": "application/json",
				...init.headers,
			},
			signal: AbortSignal.timeout(this.#timeoutMs),
		});
		const body: unknown = await response.json();
		if (!response.ok) throw new WorkError(response.status, (body as WorkErrorBody).error);
		return body as T;
	}
}
