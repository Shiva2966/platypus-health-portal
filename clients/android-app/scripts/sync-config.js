// Copies ../config/server.json into the Android web assets so the app has a build-time default URL.
const fs = require("fs");
const path = require("path");
const src = path.join(__dirname, "..", "..", "config", "server.json");
const dst = path.join(__dirname, "..", "www", "server-default.json");
fs.copyFileSync(src, dst);
console.log("Default server config copied:", fs.readFileSync(dst, "utf8").match(/"serverUrl":\s*"[^"]*"/)[0]);
