const fs = require('fs');
const path = require('path');
const supportedVersions = ['2022', '2019'];
const availableVersions = [];
for (const version of supportedVersions) {
    let vsPath = process.env[`vs${version}_install`];
    console.log(`[DEBUG] Checking for VS${version}: env var vs${version}_install = ${vsPath}`);
    if (vsPath && fs.existsSync(vsPath)) {
        console.log(`[DEBUG] Found VS${version} via env var: ${vsPath}`);
        availableVersions.push(version);
        break;
    }
}
console.log(`[DEBUG] Available versions: ${availableVersions}`);
