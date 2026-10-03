// Copies ../config/server.json -> ./server-default.json (bundled default server URL).
const fs = require("fs");
const path = require("path");
const src = path.join(__dirname, "..", "..", "config", "server.json");
const dst = path.join(__dirname, "..", "server-default.json");
fs.copyFileSync(src, dst);
console.log("Default server config copied:", fs.readFileSync(dst, "utf8").match(/"serverUrl":\s*"[^"]*"/)[0]);
