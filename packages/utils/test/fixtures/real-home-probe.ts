import * as env from "@oh-my-pi/pi-utils/env";

const keys = Object.keys(process.env)
	.filter(key => /_DOTENV_PROBE_/.test(key))
	.sort();
const passwdHome = typeof env.getPasswdHomeDir === "function" ? (env.getPasswdHomeDir() ?? null) : null;
process.stdout.write(JSON.stringify({ keys, passwdHome }));
