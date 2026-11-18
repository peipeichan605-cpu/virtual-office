"""Narrow static surface for the private mobile gateway.

HTML is deliberately excluded: the gateway separately serves reviewed mobile
entry pages with its own policy. This helper never serves the desktop owner UI,
legacy face pages, demos, test harnesses, source maps, or arbitrary JSON.
"""
from pathlib import Path
import re

# Reviewed runtime modules from the retained 3D office. New modules require an
# explicit entry here; a new file in public/ does not automatically become public.
OFFICE_FILES = frozenset({
    'office/agent-management.js',
    'office/agent.css',
    'office/agent.js',
    'office/app.js',
    'office/audio/music-loop.m4a',
    'office/audio/sound.js',
    'office/codex-model-ui.js',
    'office/commands.css',
    'office/commands.js',
    'office/feed.js',
    'office/graphics-settings-ui.js',
    'office/graphics-settings.css',
    'office/graphics-settings.js',
    'office/grok-avatar.js',
    'office/grok-connection.js',
    'office/header-actions.js',
    'office/history-detail.js',
    'office/lib/feedbase.js',
    'office/lib/state.js',
    'office/lib/util.js',
    'office/mobile-access.js',
    'office/mobile-entry.js',
    'office/mobile-pair.js',
    'office/mobile-session-controls.js',
    'office/mobile.css',
    'office/office-api.js',
    'office/office-controls.js',
    'office/overview.js',
    'office/platform-icons/codex.svg',
    'office/platform-icons/cursor.svg',
    'office/platform-icons/grok_bot.svg',
    'office/platform-icons/hermes.svg',
    'office/platform-identity.js',
    'office/platform-palette.js',
    'office/scene/actor.js',
    'office/scene/aura-cache.js',
    'office/scene/blobgen.js',
    'office/scene/building.js',
    'office/scene/daylight.js',
    'office/scene/dialogue-layout.js',
    'office/scene/dialogue-window.js',
    'office/scene/dialogue.js',
    'office/scene/floorstyle.js',
    'office/scene/follower-obstacles.js',
    'office/scene/frame-pacer.js',
    'office/scene/gaussian-kernel.js',
    'office/scene/grok-colors.js',
    'office/scene/ground-hall.js',
    'office/scene/idle-render.js',
    'office/scene/lighting-layout.js',
    'office/scene/looks.js',
    'office/scene/mesh-bounds.js',
    'office/scene/office.js',
    'office/scene/outlines.js',
    'office/scene/platform-controller.js',
    'office/scene/platform-models.js',
    'office/scene/post.js',
    'office/scene/scene.css',
    'office/scene/scene.js',
    'office/scene/spring.js',
    'office/scene/static-furniture.js',
    'office/scene/studio.js',
    'office/scene/sunpatch.js',
    'office/scene/walls.js',
    'office/seating.js',
    'office/share/share.css',
    'office/share/share.js',
    'office/simple-mode.css',
    'office/simple-mode.js',
    'office/styles.css',
    'office/subagent-view.js',
    'office/ui/ui.css',
    'office/ui/ui.js',
    'office/wallpaper.js',
})

THREE_FILES = frozenset({
    'assets/vendor/three/LICENSE',
    'assets/vendor/three/build/three.module.js',
    'assets/vendor/three/examples/jsm/environments/RoomEnvironment.js',
    'assets/vendor/three/examples/jsm/geometries/RoundedBoxGeometry.js',
    'assets/vendor/three/examples/jsm/loaders/GLTFLoader.js',
    'assets/vendor/three/examples/jsm/postprocessing/EffectComposer.js',
    'assets/vendor/three/examples/jsm/postprocessing/MaskPass.js',
    'assets/vendor/three/examples/jsm/postprocessing/OutputPass.js',
    'assets/vendor/three/examples/jsm/postprocessing/Pass.js',
    'assets/vendor/three/examples/jsm/postprocessing/RenderPass.js',
    'assets/vendor/three/examples/jsm/postprocessing/ShaderPass.js',
    'assets/vendor/three/examples/jsm/renderers/CSS2DRenderer.js',
    'assets/vendor/three/examples/jsm/shaders/CopyShader.js',
    'assets/vendor/three/examples/jsm/shaders/OutputShader.js',
    'assets/vendor/three/examples/jsm/utils/BufferGeometryUtils.js',
})

ASSET_SUFFIXES = frozenset({'.png', '.jpg', '.jpeg', '.webp', '.avif', '.glb', '.woff', '.woff2', '.m4a', '.mp3', '.ogg', '.wav'})


def mobile_static_file(public_root, raw_path):
    """Return one approved regular file, or None. Accept a canonical URL path.

    Pass the original undecoded URL path, without a query string. Any percent
    encoding is rejected; all installed asset names are ASCII. Every component
    below the trusted public root is checked for symlinks before resolution.
    """
    if not isinstance(raw_path, str) or not raw_path.startswith('/'):
        return None
    if not re.fullmatch(r'/[A-Za-z0-9_./-]{1,400}', raw_path):
        return None
    parts = raw_path[1:].split('/')
    if any(not part or part in {'.', '..'} or part.startswith('.') for part in parts):
        return None
    relative = '/'.join(parts)
    allowed = relative in OFFICE_FILES or relative in THREE_FILES
    if parts[0] == 'assets' and 'vendor' not in parts:
        allowed = Path(relative).suffix.lower() in ASSET_SUFFIXES
    if not allowed:
        return None
    try:
        root = Path(public_root)
        if root.is_symlink() or not root.is_dir():
            return None
        root = root.resolve(strict=True)
        candidate = root
        for part in parts:
            candidate = candidate / part
            if candidate.is_symlink():
                return None
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        return resolved
    except (OSError, ValueError, RuntimeError):
        return None
