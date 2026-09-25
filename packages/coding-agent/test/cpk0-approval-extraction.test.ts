import { describe, expect, it } from "bun:test";
import { Project, SyntaxKind } from "ts-morph";
import { extractApprovalFromNode } from "../../../scripts/cpk0-inventory";

declare global {
	interface MapConstructor {
		new (iterable?: any): Map<any, any>;
	}
}

describe("CPK-0 approval extraction (OMP-204-s01)", () => {
	it('extracts "write" as const property declaration to approval_tier "write", dynamic: false', () => {
		const project = new Project({ useInMemoryFileSystem: true });
		const sf = project.createSourceFile(
			"test.ts",
			`class Tool {
	approval = "write" as const;
}`,
		);
		const cls = sf.getClassOrThrow("Tool");
		const prop = cls.getProperty("approval");
		const result = extractApprovalFromNode(prop);
		expect(result).toEqual({
			approval_tier: "write",
			dynamic: false,
			tiers: ["write"],
		});
	});

	it('extracts "write" as const property assignment to approval_tier "write", dynamic: false', () => {
		const project = new Project({ useInMemoryFileSystem: true });
		const sf = project.createSourceFile(
			"test.ts",
			`const tool = {
	approval: "write" as const,
};`,
		);
		const obj = sf
			.getVariableDeclarationOrThrow("tool")
			.getInitializerIfKindOrThrow(SyntaxKind.ObjectLiteralExpression);
		const prop = obj.getProperty("approval");
		const result = extractApprovalFromNode(prop);
		expect(result).toEqual({
			approval_tier: "write",
			dynamic: false,
			tiers: ["write"],
		});
	});

	it('extracts "read" literal to approval_tier "read", dynamic: false', () => {
		const project = new Project({ useInMemoryFileSystem: true });
		const sf = project.createSourceFile(
			"test.ts",
			`class Tool {
	approval = "read";
}`,
		);
		const cls = sf.getClassOrThrow("Tool");
		const prop = cls.getProperty("approval");
		const result = extractApprovalFromNode(prop);
		expect(result).toEqual({
			approval_tier: "read",
			dynamic: false,
			tiers: ["read"],
		});
	});

	it('extracts arrow function (a => a.x ? "write" : "read") to dynamic: true and sorted tiers ["read", "write"]', () => {
		const project = new Project({ useInMemoryFileSystem: true });
		const sf = project.createSourceFile(
			"test.ts",
			`class Tool {
	approval = (a: any) => a.x ? "write" : "read";
}`,
		);
		const cls = sf.getClassOrThrow("Tool");
		const prop = cls.getProperty("approval");
		const result = extractApprovalFromNode(prop);
		expect(result).toEqual({
			approval_tier: "dynamic",
			dynamic: true,
			tiers: ["read", "write"],
		});
	});

	it('defaults missing approval (undefined or absent property) to "exec", dynamic: false', () => {
		const project = new Project({ useInMemoryFileSystem: true });
		const sf = project.createSourceFile(
			"test.ts",
			`class Tool {
	name = "sample";
}`,
		);
		const cls = sf.getClassOrThrow("Tool");
		const prop = cls.getProperty("approval"); // undefined
		const result = extractApprovalFromNode(prop);
		expect(result).toEqual({
			approval_tier: "exec",
			dynamic: false,
			tiers: ["exec"],
		});

		const missingExplicit = extractApprovalFromNode(undefined);
		expect(missingExplicit).toEqual({
			approval_tier: "exec",
			dynamic: false,
			tiers: ["exec"],
		});
	});
});
