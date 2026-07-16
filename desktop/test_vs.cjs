const fs = require('fs');
const path = require('path');
const os = require('os');

function hasSupportedVisualStudioVersion() {
    const supportedVersions = ['2022', '2019'];
    const availableVersions = [];

    for (const version of supportedVersions) {
        // Check environment variable first (explicit override)
        let vsPath = process.env[`vs${version}_install`];
        if (vsPath && fs.existsSync(vsPath)) {
            console.log(`[DEBUG] Found VS${version} via env var vs${version}_install: ${vsPath}`);
            availableVersions.push(version);
            break;
        }

        // Check default installation paths
        const programFiles86Path = process.env['ProgramFiles(x86)'];
        const programFiles64Path = process.env['ProgramFiles'];
        const vsTypes = ['Enterprise', 'Professional', 'Community', 'Preview', 'BuildTools', 'IntPreview'];

        if (programFiles64Path) {
            vsPath = `${programFiles64Path}/Microsoft Visual Studio/${version}`;
            if (vsTypes.some(vsType => fs.existsSync(path.join(vsPath, vsType)))) {
                console.log(`[DEBUG] Found VS${version} via programFiles64Path: ${vsPath}`);
                availableVersions.push(version);
                break;
            }
        }

        if (programFiles86Path) {
            vsPath = `${programFiles86Path}/Microsoft Visual Studio/${version}`;
            if (vsTypes.some(vsType => fs.existsSync(path.join(vsPath, vsType)))) {
                console.log(`[DEBUG] Found VS${version} via programFiles86Path: ${vsPath}`);
                availableVersions.push(version);
                break;
            }
        }
    }

    return availableVersions.length;
}

// Set environment variable for testing
process.env.vs2022_install = '/c/dummy/vs2022';
process.env.vs2019_install = '/c/dummy/vs2022';

console.log('VS2022 install path from env:', process.env.vs2022_install);
console.log('Does it exist?', fs.existsSync(process.env.vs2022_install));

const result = hasSupportedVisualStudioVersion();
console.log('Result:', result);