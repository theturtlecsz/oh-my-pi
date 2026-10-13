import { describe, expect, test } from "bun:test";
import * as path from "node:path";

const ROOT = path.resolve(import.meta.dir, "../..", "docs/report/ecc-database-advisor-trial");

// Baseline captured at 2836091d38f70a1fe0bc8836c9bf7aa92f900dca for the
// OMP-525 trial evidence. These bytes must not change; the retirement decision
// record (DECISION.md) is the only permitted addition to the directory.
const DECISION_FILE = "DECISION.md";

const BASELINE_SHA256: Record<string, string> = {
	"cases.json": "83336d3f7ad1e7923826a9561cd3421aba1b2784b411dded0f09d176bec689ce",
	"labels/k1.json": "b675809e9531f69201176f4a8668dfa3530cfff0625ce0229b6b3cad28e57029",
	"labels/k2.json": "d646a8008397b20f3dcfcc9e8abdd99a4058e69148fd2a4677f6072f591ddc3d",
	"labels/o1.json": "c447c09ae5c1b1d1197cbeb448866a80cf94cbd1071de5c8e80f7abffdaabdd6",
	"labels/o2.json": "379cb86737832a38c72a17f73655e47b23a183df725dd39b724ae8e21a80cf68",
	"labels/o3.json": "1a47c2224e4f02e5e78cdf71d4ecd6e16c1db6cb6512473c111fed76ba72be4e",
	"labels/o4.json": "49af7c95015ae175f6c1082669acbd22f800c86ac28312f8ff638c1230a59c4a",
	"README.md": "076a846324ac939ff6f3ea730157ccc0a9e25ae3cce4347778f593e2f828fd0c",
	"runs/k1/SHA256SUMS": "46aececd7745a5668d970fd0a7d7c04b78b84a8cdb902b276893450c3bf2cf59",
	"runs/k1/advisor-transcript.jsonl": "64efe6001ac05e975c14954799d0a1083660e631dbb2df231cf05b7e13f43fc3",
	"runs/k1/notes.json": "680259e6435fb6125a80e59ea209b624e70bcdfa11decd236a1b0403d7ae88b0",
	"runs/k1/prompt.md": "76ed19f9265782f9889c74dfcd222c9080486dcaa4a5607c86528790fcfe4aa0",
	"runs/k2/SHA256SUMS": "4b9683060c4a7c777e5a52438ec7353a3dbedcabd0a9ff33b0565e7929d055d8",
	"runs/k2/advisor-transcript.jsonl": "ad0664eb6c5c2404103285553f35c8faef00088542577c8948f30a31041d2ec5",
	"runs/k2/notes.json": "3fbdcc229a8fa54ee40473ef25671a679c6fc976b1e746424d605316cd8738d4",
	"runs/k2/prompt.md": "aa570d01d8f4cb964a34a7c28a261710fc9f21b9cd3b3ddb6dd4e29705f2f5eb",
	"runs/o1/SHA256SUMS": "bf2c0d0a6e967c0e9762f2b3237a93203f17dad5bb14aae09ae2f53e83e0a98a",
	"runs/o1/advisor-transcript.jsonl": "2a32d7996dfc88efeb12fd7fa970d3c8bba67e18c16777d8a4896d2145ee0b40",
	"runs/o1/notes.json": "8632f522674cfdba02467c58d1eed65cbf94853e9b4fa6fbb52c03008554d1e9",
	"runs/o1/prompt.md": "75b9c1b06a7b10cf6bd509d3d360f8f7427aeff0aa68e8a43fddb87c1c1cce64",
	"runs/o2/SHA256SUMS": "e60307073f0b7839ee91e2a08542f4fa72d2ea6fe5781aba213d6086bbe4e08a",
	"runs/o2/advisor-transcript.jsonl": "8a21a4bb8a9f7d2dafc078d8b619ac2ea0f3b75916f4eb852bb25c106da57654",
	"runs/o2/notes.json": "488f3962be6773ce5ae4719c384aaed09fce3e4f23518e6bd5c7c8b3a2bf209d",
	"runs/o2/prompt.md": "6ae33e9da82ccd55c6291ad9ec514fdab804625a61178322fadd21a9f94141cd",
	"runs/o3/SHA256SUMS": "41e320a6f1cb3b54731f41cffc9e92c12be4b8dfe74917c4ff40d4b231a0f055",
	"runs/o3/advisor-transcript.jsonl": "0d59ce401d9ba3153c1d8627b3d987cc97912cf2f0952e08f9ee2c5f69125207",
	"runs/o3/notes.json": "d44a4f1e59d595a08172bf84821cd5cc8deab024e29f9776d706cb1598e3bada",
	"runs/o3/prompt.md": "48149e34f6203d1d8cd416da5c1cc6c847b85e755026ed080b2a4c5ec3de72d2",
	"runs/o4/SHA256SUMS": "73a3491c674c0169a5a6a431c77698af4c9f6031701693b0881367b42847ed37",
	"runs/o4/advisor-transcript.jsonl": "3bda028d4032f5c8d4e01873786062cbb42046505a42d638e238932bd6932e53",
	"runs/o4/notes.json": "ddab80bd965502513f7e0ae439b50e774c7755a1aa82cf3974a48337a68327b5",
	"runs/o4/prompt.md": "c95df06203c85022fcc3ea617678e7b6f9f66e93e147c3b9bd414c96f2ba2ad6",
	"summary.json": "63dc2b2c3654159aa652e3cd2bd6d0a9f4c2d49f9c768b5a52bdff67193aa3c1",
};

describe("ECC database advisor trial evidence retention", () => {
	test("every baseline file is present with its recorded bytes", async () => {
		const expected = Object.entries(BASELINE_SHA256);
		expect(expected.length).toBe(33);
		for (const [rel, sha] of expected) {
			const bytes = await Bun.file(path.join(ROOT, rel)).bytes();
			expect(new Bun.CryptoHasher("sha256").update(bytes).digest("hex"), rel).toBe(sha);
		}
	});

	test("the directory holds exactly the baseline files plus DECISION.md", async () => {
		const expected = new Set([...Object.keys(BASELINE_SHA256), DECISION_FILE]);
		const found: string[] = [];
		for await (const entry of new Bun.Glob("**/*").scan({ cwd: ROOT, onlyFiles: true })) found.push(entry);
		expect(found.sort()).toEqual([...expected].sort());
	});
});
