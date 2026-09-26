// GENERATED — do not hand-edit.
// Emitter: packages/work-client/scripts/generate-research-bindings.ts
// Source: python/omp-work/src/omp_work/contracts/v1/schema.json and api-schema.json.
export type AdmitResearchCampaignCommand = {
	payload: AdmitResearchCampaignPayload;
	type: "admit_research_campaign";
};

export type AdmitResearchCampaignPayload = {
	campaign_id: string;
	compatibility: ResearchCompatibilityManifest;
	compatibility_sha256: string;
	policy_sha256: string;
	revision_id: string;
	spec_sha256: string;
	work_id: string;
};

export type AdmitResearchCampaignResult = {
	campaign: ResearchCampaign;
	status: "applied" | "replayed";
	type: "admit_research_campaign";
};

export type BindResearchDeliverableCommand = {
	payload: BindResearchDeliverablePayload;
	type: "bind_research_deliverable";
};

export type BindResearchDeliverablePayload = {
	binding_sha256: string;
	campaign_id: string;
	candidate_digest: string;
	native_candidate_id: string;
	revision_id: string;
	trial_id: string;
	work_id: string;
};

export type BindResearchDeliverableResult = {
	deliverable_binding: ResearchDeliverableBinding;
	status: "applied" | "replayed";
	type: "bind_research_deliverable";
};

export type BindResearchReceiptManifestCommand = {
	payload: BindResearchReceiptManifestPayload;
	type: "bind_research_receipt_manifest";
};

export type BindResearchReceiptManifestPayload = {
	artifact_sha256: string;
	manifest_sha256: string;
	receipt_id: string;
};

export type BindResearchReceiptManifestResult = {
	artifact_sha256: string;
	manifest_sha256: string;
	receipt_id: string;
	status: "applied" | "replayed";
	type: "bind_research_receipt_manifest";
};

export type CancelResearchCampaignCommand = {
	payload: CancelResearchCampaignPayload;
	type: "cancel_research_campaign";
};

export type CancelResearchCampaignPayload = {
	campaign_id: string;
	reason: string;
	work_id: string;
};

export type CancelResearchCampaignResult = {
	campaign: ResearchCampaign;
	status: "applied" | "replayed";
	type: "cancel_research_campaign";
};

export type ClaimResearchReplicateCommand = {
	payload: ClaimResearchReplicatePayload;
	type: "claim_research_replicate";
};

export type ClaimResearchReplicatePayload = {
	artifact_sha256: string;
};

export type ClaimResearchReplicateResult = {
	artifact_sha256: string;
	status: "applied" | "replayed";
	type: "claim_research_replicate";
};

export type CollectResearchArtifactCommand = {
	payload: CollectResearchArtifactPayload;
	type: "collect_research_artifact";
};

export type CollectResearchArtifactPayload = {
	archive_member?: string | null;
	manifest: ResearchArtifactManifest;
	manifest_sha256: string;
	relative_path: string;
};

export type CollectResearchArtifactResult = {
	artifact: ResearchArtifact;
	status: "applied" | "replayed";
	type: "collect_research_artifact";
};

export type ConcludeResearchCampaignCommand = {
	payload: ConcludeResearchCampaignPayload;
	type: "conclude_research_campaign";
};

export type ConcludeResearchCampaignPayload = {
	campaign_id: string;
	outcome: "supported" | "refuted" | "inconclusive" | "resource_exhausted" | "externally_blocked";
	policy_sha256: string;
	reason: string;
	work_id: string;
};

export type ConcludeResearchCampaignResult = {
	campaign: ResearchCampaign;
	status: "applied" | "replayed";
	type: "conclude_research_campaign";
};

export type CreateResearchCampaignCommand = {
	payload: CreateResearchCampaignPayload;
	type: "create_research_campaign";
};

export type CreateResearchCampaignPayload = {
	campaign_id: string;
	domain: ResearchDomain;
	revision_id: string;
	spec: ResearchCampaignSpec;
	spec_sha256: string;
	work_id: string;
};

export type CreateResearchCampaignResult = {
	campaign: ResearchCampaign;
	status: "applied" | "replayed";
	type: "create_research_campaign";
};

export type ProposeResearchTrialCommand = {
	payload: ProposeResearchTrialPayload;
	type: "propose_research_trial";
};

export type ProposeResearchTrialPayload = {
	action: ResearchAction;
	campaign_id: string;
	candidate_digest: string;
	decision_id: string;
	environment_sha256: string;
	evaluator_sha256: string;
	experiment_spec_sha256: string;
	hardware_class?: string | null;
	input_manifest_sha256: string;
	policy_sha256: string;
	reason?: string | null;
	resource_request?: Record<string, unknown> | null;
	seed?: number | null;
	trial_id: string;
	work_id: string;
};

export type ProposeResearchTrialResult = {
	status: "applied" | "replayed";
	trial: ResearchTrial;
	type: "propose_research_trial";
};

export type RecordResearchCacheCommand = {
	payload: RecordResearchCachePayload;
	type: "record_research_cache";
};

export type RecordResearchCachePayload = {
	artifact_sha256: string;
	cache_key: string;
};

export type RecordResearchCacheResult = {
	artifact_sha256: string;
	cache_key: string;
	status: "applied" | "replayed";
	type: "record_research_cache";
};

export type RecordResearchObservationCommand = {
	payload: RecordResearchObservationPayload;
	type: "record_research_observation";
};

export type RecordResearchObservationPayload = {
	campaign_id: string;
	commit_sha?: string | null;
	execution_status: "completed" | "crashed" | "timed_out" | "canceled" | "unknown";
	issuer_kind: "legacy_autoresearch" | "candidate_authored";
	observation_id: string;
	observed_at: string;
	payload: Record<string, unknown>;
	payload_sha256: string;
	source_ref: string;
	trial_id?: string | null;
};

export type RecordResearchObservationResult = {
	observation: ResearchObservation;
	status: "applied" | "replayed";
	type: "record_research_observation";
};

export type RegisterResearchArtifactCommand = {
	payload: RegisterResearchArtifactPayload;
	type: "register_research_artifact";
};

export type RegisterResearchArtifactPayload = {
	content_base64: string;
	manifest: ResearchArtifactManifest;
	manifest_sha256: string;
};

export type RegisterResearchArtifactResult = {
	artifact: ResearchArtifact;
	status: "applied" | "replayed";
	type: "register_research_artifact";
};

export type RegisterResearchComponentCommand = {
	payload: RegisterResearchComponentPayload;
	type: "register_research_component";
};

export type RegisterResearchComponentPayload = {
	component_sha256: string;
	descriptor: ResearchComponentDescriptor;
};

export type RegisterResearchComponentResult = {
	component: ResearchComponent;
	status: "applied" | "replayed";
	type: "register_research_component";
};

export type RegisterResearchDatasetCommand = {
	payload: RegisterResearchDatasetPayload;
	type: "register_research_dataset";
};

export type RegisterResearchDatasetPayload = {
	manifest: ResearchDatasetManifest;
	manifest_sha256: string;
};

export type RegisterResearchDatasetResult = {
	dataset: ResearchDataset;
	status: "applied" | "replayed";
	type: "register_research_dataset";
};

export type RegisterResearchSourceCommand = {
	payload: RegisterResearchSourcePayload;
	type: "register_research_source";
};

export type RegisterResearchSourcePayload = {
	manifest: ResearchSourceManifest;
	manifest_sha256: string;
};

export type RegisterResearchSourceResult = {
	source: ResearchSource;
	status: "applied" | "replayed";
	type: "register_research_source";
};

export type ResearchAction =
	| "retrieve"
	| "draft"
	| "repair"
	| "refine"
	| "challenge"
	| "combine"
	| "evaluate"
	| "replicate"
	| "deepen"
	| "prune"
	| "synthesize"
	| "escalate"
	| "conclude";

export type ResearchArtifact = {
	artifact_sha256: string;
	manifest: ResearchArtifactManifest;
	manifest_sha256: string;
	registered_at: string;
	registered_by: string;
	workspace_id: string;
};

export type ResearchArtifactContentView = {
	artifact: ResearchArtifact;
	content_base64: string;
};

export type ResearchArtifactManifest = {
	access_class: "workspace";
	artifact_sha256: string;
	contract_version: "research-artifact.v1";
	issuer_kind: "legacy_autoresearch" | "candidate_authored";
	media_type: string;
	name: string;
	size_bytes: number;
	source_ref: string;
	valid_until?: string | null;
};

export type ResearchBlockedDependency = {
	kind: "work_item" | "budget_scope" | "capability" | "external";
	reason: string;
	ref: string;
};

export type ResearchCampaign = {
	admitted_at?: string | null;
	blocked_dependency?: ResearchBlockedDependency | null;
	blocked_from_state?: "admitted" | "running" | "paused" | "evaluating" | null;
	campaign_id: string;
	cancel_reason?: string | null;
	cancelled_at?: string | null;
	compatibility?: ResearchCompatibilityManifest | null;
	compatibility_sha256?: string | null;
	concluded_at?: string | null;
	created_at: string;
	domain: ResearchDomain;
	outcome?: "supported" | "refuted" | "inconclusive" | "resource_exhausted" | "externally_blocked" | null;
	outcome_reason?: string | null;
	policy_sha256?: string | null;
	revision_id: string;
	spec: ResearchCampaignSpec;
	spec_sha256: string;
	state: "draft" | "admitted" | "running" | "paused" | "evaluating" | "blocked" | "concluded" | "cancelled";
	work_id: string;
	workspace_id: string;
};

export type ResearchCampaignSpec = {
	authorized_data_classification?: string[];
	candidate_mapping_policy: string;
	evaluation_protocol_id: string;
	evaluation_protocol_sha256: string;
	objective: string;
	resource_policy_ref: string;
	resource_vector?: ResearchResourceVector | null;
};

export type ResearchCompatibilityManifest = {
	audits?: string[];
	contract_version: "research-compatibility.v1";
	environments?: string[];
	evaluators?: string[];
	releases?: string[];
	workers?: string[];
};

export type ResearchComponent = {
	component_sha256: string;
	descriptor: ResearchComponentDescriptor;
	kind: ResearchComponentKind;
	registered_at: string;
	workspace_id: string;
};

export type ResearchComponentDescriptor = {
	artifact_sha256: string;
	capabilities?: string[];
	contract_version: "research-component.v1";
	kind: ResearchComponentKind;
	name: string;
	roles?: ResearchRole[];
	version: string;
};

export type ResearchComponentKind = "worker" | "evaluator" | "policy" | "audit" | "release" | "environment";

export type ResearchDataset = {
	artifact_sha256?: string | null;
	dataset_id: string;
	manifest: ResearchDatasetManifest;
	manifest_sha256: string;
	registered_at: string;
	registered_by: string;
	source_id: string;
	workspace_id: string;
};

export type ResearchDatasetManifest = {
	access: ResearchSourceAccess;
	artifact_sha256?: string | null;
	contract_version: "research-dataset.v1";
	dataset_id: string;
	retention_until?: string | null;
	snapshot_sha256: string;
	source_id: string;
	version: string;
};

export type ResearchDatasetView = {
	content_base64?: string | null;
	dataset: ResearchDataset;
};

export type ResearchDeliverableBinding = {
	binding_sha256: string;
	bound_at: string;
	campaign_id: string;
	candidate_digest: string;
	native_candidate_id: string;
	revision_id: string;
	trial_id: string;
	work_id: string;
	workspace_id: string;
};

export type ResearchDomain =
	| "engineering"
	| "omp_harness"
	| "machine_learning"
	| "literature"
	| "simulation"
	| "external_instrument";

export type ResearchObservation = {
	campaign_id: string;
	commit_sha?: string | null;
	execution_status: "completed" | "crashed" | "timed_out" | "canceled" | "unknown";
	issuer_kind: "legacy_autoresearch" | "candidate_authored";
	observation_id: string;
	observed_at: string;
	payload: Record<string, unknown>;
	payload_sha256: string;
	recorded_at: string;
	source_ref: string;
	trial_id?: string | null;
	workspace_id: string;
};

export type ResearchResourceVector = {
	cpu_seconds?: number | null;
	gpu_seconds?: number | null;
	input_tokens?: number | null;
	max_wall_seconds?: number | null;
	memory_mib?: number | null;
	model_calls?: number | null;
	output_tokens?: number | null;
	retrieval_requests?: number | null;
};

export type ResearchRole =
	| "campaign_planner"
	| "research_worker"
	| "hypothesis_generator"
	| "method_critic"
	| "implementer"
	| "selector"
	| "analyst"
	| "scientific_reviewer"
	| "harness_researcher"
	| "native_auditor"
	| "synthesizer";

export type ResearchSource = {
	artifact_sha256?: string | null;
	manifest: ResearchSourceManifest;
	manifest_sha256: string;
	registered_at: string;
	registered_by: string;
	source_id: string;
	status: "ok" | "inaccessible";
	workspace_id: string;
};

export type ResearchSourceAccess = {
	access_class: "workspace" | "project";
	decision?: "allow" | "deny" | null;
	project_id?: string | null;
};

export type ResearchSourceManifest = {
	access: ResearchSourceAccess;
	artifact_sha256?: string | null;
	contract_version: "research-source.v1";
	location: string;
	retention_until?: string | null;
	source_id: string;
	status: "ok" | "inaccessible";
	version: string;
};

export type ResearchSourceView = {
	content_base64?: string | null;
	source: ResearchSource;
};

export type ResearchTrial = {
	action?: ResearchAction | null;
	archived_at?: string | null;
	archived_reason?: string | null;
	campaign_id: string;
	candidate_digest: string;
	decision_id: string;
	environment_sha256: string;
	evaluator_sha256: string;
	experiment_spec_sha256: string;
	hardware_class?: string | null;
	input_manifest_sha256: string;
	policy_sha256: string;
	proposed_at: string;
	reason?: string | null;
	resource_request?: Record<string, unknown> | null;
	seed?: number | null;
	state: "proposed" | "archived";
	trial_id: string;
	work_id: string;
	workspace_id: string;
};

export type ResearchView = {
	artifacts: ResearchArtifact[];
	campaigns: ResearchCampaign[];
	components?: ResearchComponent[];
	deliverable_bindings: ResearchDeliverableBinding[];
	observations: ResearchObservation[];
	trials: ResearchTrial[];
	work_id: string;
};

export type SetResearchCampaignStateCommand = {
	payload: SetResearchCampaignStatePayload;
	type: "set_research_campaign_state";
};

export type SetResearchCampaignStatePayload = {
	blocked_dependency?: ResearchBlockedDependency | null;
	campaign_id: string;
	expected_state: "admitted" | "running" | "paused" | "evaluating" | "blocked";
	policy_sha256: string;
	target_state: "admitted" | "running" | "paused" | "evaluating" | "blocked";
	work_id: string;
};

export type SetResearchCampaignStateResult = {
	campaign: ResearchCampaign;
	status: "applied" | "replayed";
	type: "set_research_campaign_state";
};
