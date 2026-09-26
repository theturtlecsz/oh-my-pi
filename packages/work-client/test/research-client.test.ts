import { expect, test } from "bun:test";
import {
	type ResearchArtifact,
	type ResearchArtifactContentView,
	type ResearchCampaign,
	type ResearchComponent,
	type ResearchDatasetView,
	type ResearchDeliverableBinding,
	type ResearchObservation,
	type ResearchSourceView,
	type ResearchTrial,
	type ResearchView,
	WorkClient,
} from "../src/index";

const ENV = {
	api_version: "work.omp.dev/v1" as const,
	workspace_id: "00000000-0000-0000-0000-000000000001",
	operation_id: "00000000-0000-0000-0000-0000000000a1",
	request_id: "00000000-0000-0000-0000-0000000000b1",
	correlation_id: "00000000-0000-0000-0000-0000000000c1",
};

const RECEIPT = {
	operation_id: "o",
	request_id: "r",
	state: "replayed",
	request_sha256: "0".repeat(64),
	result_sha256: "1".repeat(64),
	diagnostics: [],
};

test("dispatches research commands, decodes results, and calls research view", async () => {
	let lastUrl: string | undefined;
	let lastMethod: string | undefined;

	const campaignId = "00000000-0000-0000-0000-0000000000c1";
	const workId = "00000000-0000-0000-0000-0000000000w1";
	const revisionId = "00000000-0000-0000-0000-0000000000r1";
	const trialId = "00000000-0000-0000-0000-0000000000t1";
	const observationId = "00000000-0000-0000-0000-0000000000o1";
	const candidateId = "00000000-0000-0000-0000-0000000000d1";

	const mockCampaign: ResearchCampaign = {
		campaign_id: campaignId,
		workspace_id: ENV.workspace_id,
		work_id: workId,
		revision_id: revisionId,
		domain: "machine_learning",
		spec: {
			objective: "test objective",
			evaluation_protocol_id: "proto-1",
			evaluation_protocol_sha256: "0".repeat(64),
			resource_policy_ref: "pol-1",
			candidate_mapping_policy: "map-1",
		},
		spec_sha256: "1".repeat(64),
		policy_sha256: "2".repeat(64),
		state: "admitted",
		created_at: "2026-09-17T12:00:00Z",
		admitted_at: "2026-09-17T12:01:00Z",
	};

	const mockTrial: ResearchTrial = {
		trial_id: trialId,
		workspace_id: ENV.workspace_id,
		campaign_id: campaignId,
		work_id: workId,
		decision_id: "00000000-0000-0000-0000-0000000000e1",
		candidate_digest: "3".repeat(64),
		experiment_spec_sha256: "4".repeat(64),
		evaluator_sha256: "5".repeat(64),
		environment_sha256: "6".repeat(64),
		input_manifest_sha256: "7".repeat(64),
		policy_sha256: "2".repeat(64),
		state: "proposed",
		proposed_at: "2026-09-17T12:02:00Z",
		action: "evaluate",
		reason: "test proposal",
	};

	const mockObservation: ResearchObservation = {
		observation_id: observationId,
		workspace_id: ENV.workspace_id,
		campaign_id: campaignId,
		trial_id: trialId,
		issuer_kind: "candidate_authored",
		source_ref: "subtrial/0",
		execution_status: "completed",
		payload: { score: 0.95 },
		payload_sha256: "8".repeat(64),
		observed_at: "2026-09-17T12:03:00Z",
		recorded_at: "2026-09-17T12:03:01Z",
	};

	const mockBinding: ResearchDeliverableBinding = {
		trial_id: trialId,
		workspace_id: ENV.workspace_id,
		campaign_id: campaignId,
		work_id: workId,
		revision_id: revisionId,
		candidate_digest: "3".repeat(64),
		native_candidate_id: candidateId,
		binding_sha256: "9".repeat(64),
		bound_at: "2026-09-17T12:04:00Z",
	};

	let nextResult: unknown;

	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			lastUrl = String(input);
			lastMethod = init?.method;
			return Response.json(nextResult);
		},
	);

	// 0. register_research_component
	const mockComponent: ResearchComponent = {
		component_sha256: "2".repeat(64),
		workspace_id: ENV.workspace_id,
		kind: "policy",
		descriptor: {
			contract_version: "research-component.v1",
			kind: "policy",
			name: "test-policy",
			version: "1",
			artifact_sha256: "a".repeat(64),
			roles: [],
			capabilities: [],
		},
		registered_at: new Date().toISOString(),
	};
	nextResult = {
		receipt: RECEIPT,
		result: { type: "register_research_component", status: "applied", component: mockComponent },
	};
	const regRes = await client.execute({
		...ENV,
		command: {
			type: "register_research_component",
			payload: {
				component_sha256: mockComponent.component_sha256,
				descriptor: mockComponent.descriptor,
			},
		},
	});
	expect(regRes.result.type).toBe("register_research_component");
	if (regRes.result.type === "register_research_component") {
		expect(regRes.result.status).toBe("applied");
		expect(regRes.result.component.component_sha256).toBe(mockComponent.component_sha256);
	}

	// 1. create_research_campaign
	nextResult = {
		receipt: RECEIPT,
		result: { type: "create_research_campaign", status: "applied", campaign: mockCampaign },
	};
	const createRes = await client.execute({
		...ENV,
		command: {
			type: "create_research_campaign",
			payload: {
				campaign_id: campaignId,
				work_id: workId,
				revision_id: revisionId,
				domain: "machine_learning",
				spec: mockCampaign.spec,
				spec_sha256: mockCampaign.spec_sha256,
			},
		},
	});
	expect(createRes.result.type).toBe("create_research_campaign");
	if (createRes.result.type === "create_research_campaign") {
		expect(createRes.result.status).toBe("applied");
		expect(createRes.result.campaign.campaign_id).toBe(campaignId);
	}

	// 2. admit_research_campaign
	nextResult = {
		receipt: RECEIPT,
		result: { type: "admit_research_campaign", status: "applied", campaign: mockCampaign },
	};
	const admitRes = await client.execute({
		...ENV,
		command: {
			type: "admit_research_campaign",
			payload: {
				campaign_id: campaignId,
				work_id: workId,
				revision_id: revisionId,
				spec_sha256: mockCampaign.spec_sha256,
				policy_sha256: "2".repeat(64),
				compatibility: {
					contract_version: "research-compatibility.v1",
					workers: [],
					evaluators: ["5".repeat(64)],
					audits: [],
					releases: [],
					environments: ["6".repeat(64)],
				},
				compatibility_sha256: "c".repeat(64),
			},
		},
	});
	expect(admitRes.result.type).toBe("admit_research_campaign");

	// 3. propose_research_trial
	nextResult = { receipt: RECEIPT, result: { type: "propose_research_trial", status: "applied", trial: mockTrial } };
	const trialRes = await client.execute({
		...ENV,
		command: {
			type: "propose_research_trial",
			payload: {
				trial_id: trialId,
				campaign_id: campaignId,
				work_id: workId,
				decision_id: "00000000-0000-0000-0000-0000000000e1",
				action: "evaluate",
				reason: "test proposal",
				candidate_digest: "3".repeat(64),
				experiment_spec_sha256: "4".repeat(64),
				evaluator_sha256: "5".repeat(64),
				environment_sha256: "6".repeat(64),
				input_manifest_sha256: "7".repeat(64),
				policy_sha256: "2".repeat(64),
			},
		},
	});
	expect(trialRes.result.type).toBe("propose_research_trial");

	// 4. record_research_observation
	nextResult = {
		receipt: RECEIPT,
		result: { type: "record_research_observation", status: "applied", observation: mockObservation },
	};
	const obsRes = await client.execute({
		...ENV,
		command: {
			type: "record_research_observation",
			payload: {
				observation_id: observationId,
				campaign_id: campaignId,
				trial_id: trialId,
				issuer_kind: "candidate_authored",
				source_ref: "subtrial/0",
				execution_status: "completed",
				payload: { score: 0.95 },
				payload_sha256: "8".repeat(64),
				observed_at: "2026-09-17T12:03:00Z",
			},
		},
	});
	expect(obsRes.result.type).toBe("record_research_observation");

	// 5. bind_research_deliverable
	nextResult = {
		receipt: RECEIPT,
		result: { type: "bind_research_deliverable", status: "applied", deliverable_binding: mockBinding },
	};
	const bindRes = await client.execute({
		...ENV,
		command: {
			type: "bind_research_deliverable",
			payload: {
				trial_id: trialId,
				campaign_id: campaignId,
				work_id: workId,
				revision_id: revisionId,
				candidate_digest: "3".repeat(64),
				native_candidate_id: candidateId,
				binding_sha256: "9".repeat(64),
			},
		},
	});
	expect(bindRes.result.type).toBe("bind_research_deliverable");

	// 6. cancel_research_campaign
	nextResult = {
		receipt: RECEIPT,
		result: {
			type: "cancel_research_campaign",
			status: "applied",
			campaign: { ...mockCampaign, state: "cancelled" },
		},
	};
	const cancelRes = await client.execute({
		...ENV,
		command: {
			type: "cancel_research_campaign",
			payload: {
				campaign_id: campaignId,
				work_id: workId,
				reason: "testing cancellation",
			},
		},
	});
	expect(cancelRes.result.type).toBe("cancel_research_campaign");

	// 7. set_research_campaign_state (blocked variant)
	const mockBlockedCampaign: ResearchCampaign = {
		...mockCampaign,
		state: "blocked",
		blocked_dependency: {
			kind: "budget_scope",
			ref: "scope-1",
			reason: "waiting on allocation",
		},
		blocked_from_state: "running",
	};
	nextResult = {
		receipt: RECEIPT,
		result: {
			type: "set_research_campaign_state",
			status: "applied",
			campaign: mockBlockedCampaign,
		},
	};
	const setRes = await client.execute({
		...ENV,
		command: {
			type: "set_research_campaign_state",
			payload: {
				campaign_id: campaignId,
				work_id: workId,
				expected_state: "running",
				target_state: "blocked",
				policy_sha256: "2".repeat(64),
				blocked_dependency: {
					kind: "budget_scope",
					ref: "scope-1",
					reason: "waiting on allocation",
				},
			},
		},
	});
	expect(setRes.result.type).toBe("set_research_campaign_state");
	if (setRes.result.type === "set_research_campaign_state") {
		expect(setRes.result.campaign.state).toBe("blocked");
		expect(setRes.result.campaign.blocked_dependency?.kind).toBe("budget_scope");
	}

	// 8. conclude_research_campaign
	const mockConcludedCampaign: ResearchCampaign = {
		...mockCampaign,
		state: "concluded",
		outcome: "supported",
		outcome_reason: "evidence supported",
		concluded_at: "2026-09-17T12:05:00Z",
	};
	nextResult = {
		receipt: RECEIPT,
		result: {
			type: "conclude_research_campaign",
			status: "applied",
			campaign: mockConcludedCampaign,
		},
	};
	const concludeRes = await client.execute({
		...ENV,
		command: {
			type: "conclude_research_campaign",
			payload: {
				campaign_id: campaignId,
				work_id: workId,
				policy_sha256: "2".repeat(64),
				outcome: "supported",
				reason: "evidence supported",
			},
		},
	});
	expect(concludeRes.result.type).toBe("conclude_research_campaign");
	if (concludeRes.result.type === "conclude_research_campaign") {
		expect(concludeRes.result.campaign.state).toBe("concluded");
		expect(concludeRes.result.campaign.outcome).toBe("supported");
	}

	// 9. client.research(key)
	const mockArtifact: ResearchArtifact = {
		artifact_sha256: "3".repeat(64),
		manifest_sha256: "4".repeat(64),
		manifest: {
			contract_version: "research-artifact.v1",
			name: "test-artifact",
			size_bytes: 12,
			media_type: "text/plain",
			source_ref: "repo://tests/test.txt",
			access_class: "workspace",
			issuer_kind: "candidate_authored",
			artifact_sha256: "3".repeat(64),
		},
		registered_by: "test-user",
		workspace_id: ENV.workspace_id,
		registered_at: new Date().toISOString(),
	};
	const mockResearchView: ResearchView = {
		work_id: workId,
		campaigns: [mockCampaign],
		trials: [mockTrial],
		observations: [mockObservation],
		deliverable_bindings: [mockBinding],
		artifacts: [mockArtifact],
	};
	nextResult = mockResearchView;
	const view = await client.research("test-work-key");
	expect(lastUrl).toBe("http://127.0.0.1:54322/v1/work-items/test-work-key/research");
	expect(lastMethod).toBe("GET");
	expect(view.work_id).toBe(workId);
	expect(view.campaigns.length).toBe(1);
	expect(view.trials.length).toBe(1);
	expect(view.observations.length).toBe(1);
	expect(view.deliverable_bindings.length).toBe(1);
	expect(view.artifacts.length).toBe(1);

	// 10. client.researchArtifact(sha)
	const mockArtifactView: ResearchArtifactContentView = {
		artifact: mockArtifact,
		content_base64: "aGVsbG8gd29ybGQ=",
	};
	nextResult = mockArtifactView;
	const artView = await client.researchArtifact(mockArtifact.artifact_sha256);
	expect(lastUrl).toBe(
		`http://127.0.0.1:54322/v1/workspaces/${ENV.workspace_id}/research-artifacts/${mockArtifact.artifact_sha256}`,
	);
	expect(lastMethod).toBe("GET");
	expect(artView.artifact.artifact_sha256).toBe(mockArtifact.artifact_sha256);
	expect(artView.content_base64).toBe("aGVsbG8gd29ybGQ=");

	// 11. client.researchSource(sourceId, projectId)
	const sourceId = "00000000-0000-0000-0000-0000000000s1";
	const projectId = "00000000-0000-0000-0000-0000000000p1";
	const mockSourceView: ResearchSourceView = {
		source: {
			source_id: sourceId,
			workspace_id: ENV.workspace_id,
			status: "ok",
			manifest_sha256: "5".repeat(64),
			manifest: {
				contract_version: "research-source.v1",
				source_id: sourceId,
				version: "1.0",
				location: "https://example.com/source",
				status: "ok",
				access: { access_class: "workspace" },
			},
			registered_by: "test-user",
			registered_at: new Date().toISOString(),
		},
		content_base64: null,
	};
	nextResult = mockSourceView;
	const srcView = await client.researchSource(sourceId, projectId);
	expect(lastUrl).toBe(
		`http://127.0.0.1:54322/v1/workspaces/${ENV.workspace_id}/research-sources/${sourceId}/projects/${projectId}`,
	);
	expect(lastMethod).toBe("GET");
	expect(srcView.source.source_id).toBe(sourceId);

	// 12. client.researchDataset(datasetId, projectId)
	const datasetId = "00000000-0000-0000-0000-0000000000e1";
	const mockDatasetView: ResearchDatasetView = {
		dataset: {
			dataset_id: datasetId,
			source_id: sourceId,
			workspace_id: ENV.workspace_id,
			manifest_sha256: "6".repeat(64),
			manifest: {
				contract_version: "research-dataset.v1",
				dataset_id: datasetId,
				source_id: sourceId,
				version: "1.0",
				snapshot_sha256: "7".repeat(64),
				access: { access_class: "workspace" },
			},
			registered_by: "test-user",
			registered_at: new Date().toISOString(),
		},
		content_base64: null,
	};
	nextResult = mockDatasetView;
	const dsView = await client.researchDataset(datasetId, projectId);
	expect(lastUrl).toBe(
		`http://127.0.0.1:54322/v1/workspaces/${ENV.workspace_id}/research-datasets/${datasetId}/projects/${projectId}`,
	);
	expect(lastMethod).toBe("GET");
	expect(dsView.dataset.dataset_id).toBe(datasetId);
});
