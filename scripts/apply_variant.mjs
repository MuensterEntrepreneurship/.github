// Patch an extension's manifest for one variant from its variants.json, in place.
//
// Used by the release workflow to build several .mcpb from one source: a Claude
// Desktop extension holds a single user_config, so one installation means one
// account. Where separate installations are wanted - so that each one reaches only
// its own account and keeps its own keychain entry - the difference between them is
// nothing but identity and prefilled defaults.
//
// Only these fields are touched: name, display_name, the leading sentence of the
// two descriptions, the default of the EWS endpoint, and the mailbox hint appended
// to every tool description. The hint matters because all variants export the same
// tool names, and the description is what a model reads when it picks between them.
//
//   node apply_variant.mjs <extension-dir> <variant-key>
// Loaded at run time by MuensterEntrepreneurship/.github/.github/workflows/release.yml,
// at the commit that workflow itself runs from; in the extension repos the extension dir
// is the repo root (".").

import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const [dir, key] = process.argv.slice(2);
if (!dir || !key) {
  console.error("usage: apply_variant.mjs <extension-dir> <variant-key>");
  process.exit(2);
}

const variantsPath = join(dir, "variants.json");
const manifestPath = join(dir, "manifest.json");
const { variants } = JSON.parse(readFileSync(variantsPath, "utf8"));
const variant = variants.find((entry) => entry.key === key);
if (!variant) {
  console.error(
    `no variant ${JSON.stringify(key)} in ${variantsPath}; ` +
      `have: ${variants.map((v) => v.key).join(", ")}`,
  );
  process.exit(1);
}

const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));

manifest.name = variant.name;
manifest.display_name = variant.display_name;

// Say which mailbox this bundle is for, first thing, in both description fields.
const prefix = `${variant.mailbox}. `;
manifest.description = prefix + manifest.description;
manifest.long_description = prefix + manifest.long_description;

if (variant.ews_url) {
  const field = manifest.user_config?.ews_url;
  if (!field) {
    console.error("manifest has no user_config.ews_url to prefill");
    process.exit(1);
  }
  field.default = variant.ews_url;
}

// Every variant exports the same tool names, so the mailbox goes into each
// description - that line is the only thing distinguishing them in a tool list.
for (const tool of manifest.tools ?? []) {
  tool.description = `${tool.description} [${variant.mailbox}]`;
}

writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
console.log(
  `${key}: name=${manifest.name} display_name=${manifest.display_name} ` +
    `endpoint=${variant.ews_url ?? "(unverändert)"}`,
);
