"""Built-in FilmCraft recipes (v1). Each states plainly what FilmCraft can't do (see filmcraft_features)."""
from __future__ import annotations

BUILTIN_RECIPES: dict[str, dict] = {
    "travel-montage": {
        "v": 1, "name": "Travel montage",
        "description": "A short montage from the project's clips, with dissolves and a colour look.",
        "prompt": "Make a {seconds}-second travel montage on a new {width}x{height} sequence from the clips in the project: "
                  "pick the best moments, keep the order that tells the trip, add {transition_frames}-frame cross dissolves "
                  "between shots, put the music under it fitted to the length with fades in and out, and apply the colour "
                  "look \"{look}\" to every shot.",
        "params": {"seconds": "30", "width": "1920", "height": "1080", "transition_frames": "12", "look": "Golden Dusk"},
        "created_from_steps": [],
        "support": "Supported (no highlight detection: the AI picks the moments).",
    },
    "short-from-this": {
        "v": 1, "name": "Short from this",
        "description": "A vertical 9:16 Short from the current sequence, with captions.",
        "prompt": "Make a 9:16 Short from this sequence: a new {width}x{height} sequence with the best {min_seconds} to "
                  "{max_seconds} seconds, each clip scaled and positioned to fill the vertical frame, and captions from the "
                  "speech styled {caption_style}.",
        "params": {"width": "1080", "height": "1920", "min_seconds": "30", "max_seconds": "60",
                   "caption_style": "bold yellow, bottom center"},
        "created_from_steps": [],
        "support": "Partly: FilmCraft has no auto-reframe (fixed scale/position per clip) and captions need a speech model.",
    },
    "podcast-clip": {
        "v": 1, "name": "Podcast clip",
        "description": "A short clip from a talking recording: the strongest passage, tightened, captioned.",
        "prompt": "From the talking clips, make a {seconds}-second podcast clip: transcribe, keep the strongest passage, remove "
                  "pauses longer than {pause} seconds and filler words, add captions styled {caption_style}, and duck any "
                  "music under the voice.",
        "params": {"seconds": "45", "pause": "0.6", "caption_style": "white, bottom center"},
        "created_from_steps": [],
        "support": "Needs a speech model for the transcript (downloading one is held for your OK).",
    },
    "clean-talking-video": {
        "v": 1, "name": "Clean talking video",
        "description": "Remove pauses and filler words, even out the voice, add captions.",
        "prompt": "Clean up the talking video: transcribe it, remove pauses longer than {pause} seconds (keep {keep} seconds), "
                  "remove filler words, mark the voice as dialogue and auto-match its loudness, and add captions styled "
                  "{caption_style}.",
        "params": {"pause": "0.5", "keep": "0.15", "caption_style": "white, bottom center"},
        "created_from_steps": [],
        "support": "Needs a speech model for the transcript (downloading one is held for your OK).",
    },
    "youtube-qc": {
        "v": 1, "name": "YouTube QC",
        "description": "A read-only problem report before upload. Changes nothing.",
        "prompt": "Check this sequence for problems and report them as a short list; change nothing: gaps on video tracks, "
                  "audio clips peaking above {peak_db} dBFS, clips whose frame rate differs from the sequence, and offline "
                  "media. Say plainly which checks FilmCraft can't do (black or flash frames, loudness against {lufs} LUFS).",
        "params": {"peak_db": "-1", "lufs": "-14"},
        "created_from_steps": [],
        "support": "Partly: FilmCraft can't detect black/flash frames or measure loudness (LUFS).",
    },
}
