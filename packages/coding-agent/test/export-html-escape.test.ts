import { describe, expect, test } from "bun:test";
import * as vm from "node:vm";
import { type Document, parseHTML } from "@oh-my-pi/pi-utils/dom";
import { Marked } from "@oh-my-pi/pi-utils/marked";

const [templateHtml, templateJs] = await Promise.all([
	Bun.file(new URL("../src/export/html/template.html", import.meta.url)).text(),
	Bun.file(new URL("../src/export/html/template.js", import.meta.url)).text(),
]);

interface MinimalMessageEntry {
	type: "message";
	id: string;
	parentId: string | null;
	timestamp: string;
	message: {
		role: "user" | "assistant";
		content: string | unknown[];
		timestamp: number;
	};
}

interface MinimalSession {
	header: {
		type: "session";
		version: number;
		id: string;
		timestamp: string;
		cwd: string;
	};
	entries: MinimalMessageEntry[];
	leafId: string;
}

function renderSession(session: MinimalSession): Document {
	const { document, window } = parseHTML(templateHtml);
	const sessionData = document.getElementById("session-data");
	if (!sessionData) throw new Error("Export template is missing session data");
	sessionData.textContent = Buffer.from(JSON.stringify(session)).toBase64();
	Object.defineProperty(window, "location", {
		value: new URL("https://example.test/export.html"),
		configurable: true,
	});
	Object.defineProperty(window, "matchMedia", {
		value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
		configurable: true,
	});
	const themeSelect = document.getElementById("theme-select");
	if (themeSelect) {
		let themeValue = "auto";
		Object.defineProperty(themeSelect, "value", {
			get: () => themeValue,
			set: next => {
				themeValue = String(next);
			},
			configurable: true,
		});
	}

	const context = vm.createContext({
		window,
		document,
		marked: new Marked(),
		hljs: {
			getLanguage: () => false,
			highlight: (code: string) => ({
				value: code
					.replaceAll("&", "&amp;")
					.replaceAll("<", "&lt;")
					.replaceAll(">", "&gt;")
					.replaceAll('"', "&quot;"),
			}),
			highlightAuto: (code: string) => ({
				value: code
					.replaceAll("&", "&amp;")
					.replaceAll("<", "&lt;")
					.replaceAll(">", "&gt;")
					.replaceAll('"', "&quot;"),
			}),
		},
		URL,
		URLSearchParams,
		TextDecoder,
		Uint8Array,
		atob,
		navigator: { clipboard: null },
		localStorage: { getItem: () => null, setItem() {} },
		setTimeout: () => 0,
		clearTimeout() {},
	});
	vm.runInContext(templateJs, context);
	return document;
}

function createSession(entries: MinimalMessageEntry[], leafId: string, id: string): MinimalSession {
	return {
		header: {
			type: "session",
			version: 3,
			id,
			timestamp: "2026-01-01T00:00:00.000Z",
			cwd: "/tmp",
		},
		entries,
		leafId,
	};
}

describe("HTML export escape", () => {
	test("renders raw HTML tags in user Markdown as inert text", () => {
		const document = renderSession(
			createSession(
				[
					{
						type: "message",
						id: "message-1",
						parentId: null,
						timestamp: "2026-01-01T00:00:00.000Z",
						message: {
							role: "user",
							content: "hi <script>window.x=1</script> <img src=x onerror=alert(1)>",
							timestamp: 0,
						},
					},
				],
				"message-1",
				"user-raw-html-test",
			),
		);

		const rendered = document.querySelector(".markdown-content");
		if (!rendered) throw new Error("Missing .markdown-content in user message");

		expect(rendered.querySelector("script")).toBeNull();
		expect(rendered.querySelector("[onerror]")).toBeNull();
		expect(rendered.querySelector("img")).toBeNull();
		expect(rendered.textContent).toContain("<script>window.x=1</script>");
		expect(rendered.textContent).toContain("<img src=x onerror=alert(1)>");
	});

	test("renders assistant raw block HTML as inert text without active handlers", () => {
		const document = renderSession(
			createSession(
				[
					{
						type: "message",
						id: "message-2",
						parentId: null,
						timestamp: "2026-01-01T00:00:00.000Z",
						message: {
							role: "assistant",
							content: [{ type: "text", text: "<div onclick=x>\nhello\n</div>" }],
							timestamp: 0,
						},
					},
				],
				"message-2",
				"assistant-raw-block-test",
			),
		);

		const rendered = document.querySelector(".assistant-text");
		if (!rendered) throw new Error("Missing .assistant-text in assistant message");

		expect(rendered.querySelector("div[onclick]")).toBeNull();
		expect(rendered.querySelectorAll("div").length).toBe(0);
		expect(rendered.textContent).toContain("<div onclick=x>");
	});

	test("renders fenced and inline code unchanged without double escaping", () => {
		const document = renderSession(
			createSession(
				[
					{
						type: "message",
						id: "message-3",
						parentId: null,
						timestamp: "2026-01-01T00:00:00.000Z",
						message: {
							role: "user",
							content: '```ts\nconst x = "<script>alert(1)</script>";\n```\n\nHere is `x = "<script>"` in code.',
							timestamp: 0,
						},
					},
				],
				"message-3",
				"code-escaping-test",
			),
		);

		const fencedCode = document.querySelector(".markdown-content pre code");
		expect(fencedCode?.textContent).toBe('const x = "<script>alert(1)</script>";');

		const inlineCode = document.querySelector(".markdown-content p code");
		expect(inlineCode?.textContent).toBe('x = "<script>"');
	});

	test("escapes mimeType in user and assistant image tags to prevent attribute breakout", () => {
		const document = renderSession(
			createSession(
				[
					{
						type: "message",
						id: "message-user-img",
						parentId: null,
						timestamp: "2026-01-01T00:00:00.000Z",
						message: {
							role: "user",
							content: [{ type: "image", mimeType: 'image/png" onerror="alert(1)', data: "AAAA" }],
							timestamp: 0,
						},
					},
					{
						type: "message",
						id: "message-asst-img",
						parentId: "message-user-img",
						timestamp: "2026-01-01T00:00:01.000Z",
						message: {
							role: "assistant",
							content: [{ type: "image", mimeType: 'image/png" onerror="alert(2)', data: "BBBB" }],
							timestamp: 1,
						},
					},
				],
				"message-asst-img",
				"image-mimetype-escape-test",
			),
		);

		expect(document.querySelectorAll("[onerror]").length).toBe(0);

		const images = document.querySelectorAll(".message-image");
		expect(images.length).toBe(2);
		expect(images[0]?.getAttribute("src")).toBe('data:image/png" onerror="alert(1);base64,AAAA');
		expect(images[1]?.getAttribute("src")).toBe('data:image/png" onerror="alert(2);base64,BBBB');
	});
});
