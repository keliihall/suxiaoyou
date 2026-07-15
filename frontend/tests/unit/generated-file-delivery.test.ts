import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { artifactTypeFromExtension } from "../../src/lib/artifacts.ts";

test("audio and video outputs have native preview types", () => {
  for (const extension of ["mp3", "wav", "m4a", "aac", "flac", "ogg", "opus"]) {
    assert.equal(artifactTypeFromExtension(`/workspace/output.${extension}`), "audio");
  }
  for (const extension of ["mp4", "webm", "mov", "mkv", "avi"]) {
    assert.equal(artifactTypeFromExtension(`/workspace/output.${extension}`), "video");
  }

  const filePreview = readFileSync(
    "src/components/artifacts/renderers/file-preview-renderer.tsx",
    "utf8",
  );
  const mediaPreview = readFileSync(
    "src/components/artifacts/renderers/media-renderer.tsx",
    "utf8",
  );
  assert.match(filePreview, /type === "audio" \|\| type === "video"/);
  assert.match(mediaPreview, /<audio[\s\S]*controls/);
  assert.match(mediaPreview, /<video[\s\S]*controls/);
  assert.match(mediaPreview, /MAX_MEDIA_PREVIEW_BYTES = 50 \* 1024 \* 1024/);
  assert.match(mediaPreview, /URL\.revokeObjectURL\(objectUrl\)/);
});

test("shell and plugin artifact metadata render deterministic file cards", () => {
  const messageContent = readFileSync(
    "src/components/messages/message-content.tsx",
    "utf8",
  );
  const messagePresentation = readFileSync(
    "src/lib/message-presentation.ts",
    "utf8",
  );

  assert.match(messagePresentation, /FILE_CARD_TOOL_PARTS[\s\S]*"bash"/);
  assert.match(messagePresentation, /GENERATED_FILE_TOOL_PARTS[\s\S]*"bash"/);
  assert.match(messagePresentation, /"artifact_files", "written_files"/);
  assert.match(messagePresentation, /"\.mp3"/);
  assert.match(messagePresentation, /NON_USER_FACING_PATH_SEGMENTS/);
  assert.match(messagePresentation, /isExplicitArtifactFile/);
  assert.match(messagePresentation, /collectToolFilePaths\(part as ToolPart\)\.length > 0/);
  assert.match(messageContent, /hasVisibleMessageOutput\(parts\)/);

  const streamRegistry = readFileSync("src/lib/session-stream-registry.ts", "utf8");
  assert.match(streamRegistry, /Array\.isArray\(resultMetadata\.artifact_files\)/);
  assert.match(streamRegistry, /Array\.isArray\(resultMetadata\.written_files\)/);
  assert.match(streamRegistry, /"code_execute"/);
});
