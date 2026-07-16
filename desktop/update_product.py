import json
import os

product_path = 'product.json'
backup_path = 'product.json.backup'

# Backup
if not os.path.exists(backup_path):
    with open(product_path, 'r', encoding='utf-8') as f:
        content = f.read()
    with open(backup_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print('Backup created.')

# Load the JSON
with open(product_path, 'r', encoding='utf-8') as f:
    data = json.load(f)

# Update the fields as per the requirement
data['nameShort'] = 'PulseCodeAI'
data['nameLong'] = 'PulseCodeAI IDE'
data['applicationName'] = 'pulse-code'
data['win32DirName'] = 'PulseCodeAI'
data['win32NameVersion'] = 'PulseCodeAI'
data['win32RegValueName'] = 'PulseCodeAI'
# For the App IDs, we keep the same format but note that in a real product we would generate new GUIDs.
data['win32x64AppId'] = '{{D77B7E06-80BA-4137-BCF4-654B95CCEBC5}}'
data['win32arm64AppId'] = '{{D1ACE434-89C5-48D1-88D3-E2991DF85475}}'
data['win32x64UserAppId'] = '{{CC6B787D-37A0-49E8-AE24-8559A032BE0C}}'
data['win32arm64UserAppId'] = '{{3AEBF0C8-F733-4AD4-BADE-FDB816D53D7B}}'
data['win32AppUserModelId'] = 'PulseCodeAI.PulseCodeAI'
data['win32ShellNameShort'] = 'Pulse&CodeAI'
data['win32TunnelServiceMutex'] = 'pulseai-tunnelservice'
data['win32TunnelMutex'] = 'pulseai-tunnel'
data['darwinBundleIdentifier'] = 'com.pulsecodeai.code'
data['darwinProfileUUID'] = '47827DD9-4734-49A0-AF80-7E19B11495CC'  # Keep original? We'll keep the format.
data['darwinProfilePayloadUUID'] = 'CF808BE7-53F3-46C6-A7E2-7EDB98A5E959'
data['linuxIconName'] = 'pulse-code'
data['licenseName'] = 'MIT'
data['licenseUrl'] = 'https://github.com/pulsecodeai/vscode/blob/main/LICENSE.txt'
data['serverLicenseUrl'] = 'https://github.com/pulsecodeai/vscode/blob/main/LICENSE.txt'
data['serverLicense'] = []
data['serverGreeting'] = []
data['serverApplicationName'] = 'pulse-server'
data['serverDataFolderName'] = '.pulse-server'
data['tunnelApplicationName'] = 'pulse-tunnel'
data['reportIssueUrl'] = 'https://github.com/pulsecodeai/vscode/issues/new'
data['agentsTelemetryAppName'] = 'pulse'

# Update defaultChatAgent to use our own extension IDs (placeholders)
data['defaultChatAgent'] = {
    "extensionId": "PulseCodeAI.agent",
    "chatExtensionId": "PulseCodeAI.agent-chat",
    "chatExtensionOutputId": "PulseCodeAI.agent-chat.PulseCodeAI Agent.log",
    "chatExtensionOutputExtensionStateCommand": "pulsecodeai.agent.debug.extensionState",
    "documentationUrl": "https://pulsecodeai.github.io/docs/agent-overview",
    "termsStatementUrl": "https://pulsecodeai.github.io/terms",
    "privacyStatementUrl": "https://pulsecodeai.github.io/privacy",
    "skusDocumentationUrl": "https://pulsecodeai.github.io/plans",
    "optimizeUsageDocumentationUrl": "https://pulsecodeai.github.io/token-usage-tips",
    "publicCodeMatchesUrl": "https://pulsecodeai.github.io/public-code-matches",
    "managePlanUrl": "https://pulsecodeai.github.io/manage-plan",
    "upgradePlanUrl": "https://pulsecodeai.github.io/upgrade-plan",
    "signUpUrl": "https://pulsecodeai.github.io/sign-up",
    "provider": {
        "default": {
            "id": "pulse",
            "name": "Pulse"
        },
        "enterprise": {
            "id": "pulse-enterprise",
            "name": "Pulse Enterprise"
        },
        "google": {
            "id": "google",
            "name": "Google"
        },
        "apple": {
            "id": "apple",
            "name": "Apple"
        }
    },
    "providerExtensionId": "vscode.pulse-auth",
    "providerUriSetting": "pulse-enterprise.uri",
    "providerScopes": [
        ["read:user", "user:email", "repo", "workflow"],
        ["user:email"],
        ["read:user"]
    ],
    "entitlementUrl": "https://api.pulsecodeai.com/user",
    "entitlementSignupLimitedUrl": "https://api.pulsecodeai.com/subscribe_limited_user",
    "chatQuotaExceededContext": "pulsecodeai.chat.quotaExceeded",
    "completionsQuotaExceededContext": "pulsecodeai.completions.quotaExceeded",
    "walkthroughCommand": "pulsecodeai.agent.open.walkthrough",
    "completionsMenuCommand": "pulsecodeai.agent.toggleStatusMenu",
    "chatRefreshTokenCommand": "pulsecodeai.agent.refreshToken",
    "generateCommitMessageCommand": "pulsecodeai.agent.git.generateCommitMessage",
    "resolveMergeConflictsCommand": "pulsecodeai.agent.git.resolveMergeConflicts",
    "completionsAdvancedSetting": "pulsecodeai.agent.advanced",
    "completionsEnablementSetting": "pulsecodeai.agent.enable",
    "nextEditSuggestionsSetting": "pulsecodeai.agent.nextEditSuggestions.enabled",
    "tokenEntitlementUrl": "https://api.pulsecodeai.com/v2/token",
    "mcpRegistryDataUrl": "https://api.pulsecodeai.com/mcp_registry",
    "managedSettingsUrl": "https://api.pulsecodeai.com/managed_settings"
}

# Update trustedExtensionAuthAccess
data['trustedExtensionAuthAccess'] = {
    "pulse": [
        "PulseCodeAI.agent-chat"
    ],
    "pulse-enterprise": [
        "PulseCodeAI.agent-chat"
    ]
}

# Remove GitHub Copilot extensions from builtInExtensions
new_built_in = []
for ext in data.get('builtInExtensions', []):
    if ext['name'] not in ['ms-vscode.js-debug-companion', 'ms-vscode.js-debug', 'ms-vscode.vscode-js-profile-table']:
        new_built_in.append(ext)
data['builtInExtensions'] = new_built_in

# Clear builtInExtensionsEnabledWithAutoUpdates
data['builtInExtensionsEnabledWithAutoUpdates'] = []

# Write back
with open(product_path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=2)

print('Product.json updated successfully.')
# Print a few fields to verify
print('New nameShort:', data['nameShort'])
print('New applicationName:', data['applicationName'])
