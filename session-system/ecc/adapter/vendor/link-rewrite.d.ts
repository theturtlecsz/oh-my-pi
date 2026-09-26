export interface InstallIndex {
	/** source path -> installed path for files placed by the plan. */
	byFile: Map<string, string>;
	/** source dir -> installed dir for namespaced (prefix-inserted) directories. */
	byDir: Map<string, string>;
}

export interface FileMapping {
	sourceRel: string;
	destRel: string;
}

export declare function buildInstallIndex(fileMappings: FileMapping[]): InstallIndex;

export declare function rewriteRelativeLinks(
	content: string,
	options: { sourceRel: string; index: InstallIndex },
): string;
