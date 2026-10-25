import "@oh-my-pi/pi-utils/env";

process.stdout.write(JSON.stringify(Object.keys(process.env).filter(key => /_DOTENV_PROBE_/.test(key)).sort()));
