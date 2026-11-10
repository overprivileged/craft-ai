"""ROUND 2 feature map: each starter feature mapped to REAL FilmCraft v0.19.0 commands (from command_list).
Anything FilmCraft has no command for is marked "not supported" and is never faked.
Used by the guide (so the AI finds the right commands and says plainly what it can't do) and by recipes."""
from __future__ import annotations

# status: "supported" | "partly" | "not supported"; `missing` names what FilmCraft lacks.
FEATURES: list[dict] = [
    {
        "id": "short", "name": "Make a Short (9:16)", "status": "partly",
        "commands": ["file.newSequence {width:1080,height:1920}", "sequence.settings", "timeline.place",
                     "clip.sceneEditDetection", "effects.setParam motion scale/position", "transcript.createCaptions"],
        "missing": "auto-reframe (no reframe command: framing is a fixed scale/position per clip) and "
                   "picking the 'best' moment (no highlight detection: the AI chooses from scenes/transcript)",
    },
    {
        "id": "captions", "name": "Auto captions from speech + style", "status": "supported",
        "commands": ["transcript.generate (or sequence.transcribe)", "transcript.createCaptions",
                     "captions.setStyle {fontStyle,size,color,anchor:bottom,align:center,outline}"],
        "missing": "",
        "needs": "FilmCraft built with its `whisper` feature and a speech model (transcript.downloadModel is held "
                 "for the user's OK: it downloads ~150-290 MB)",
    },
    {
        "id": "silences", "name": "Cut silences / filler words", "status": "supported",
        "commands": ["transcript.generate", "transcript.removePauses {minSeconds,keepSeconds}", "transcript.removeFillers"],
        "missing": "",
        "needs": "a transcript (speech model, see captions)",
    },
    {
        "id": "music", "name": "Music: duck, fit, fades", "status": "supported",
        "commands": ["essentialSound.setType dialogue|music", "essentialSound.generateDucking",
                     "clip.remix {seconds} (fit music to length)",
                     "sequence.applyAudioTransition {effect:exponential_fade,edge:in|out}"],
        "missing": "",
    },
    {
        "id": "look", "name": "Look: warm / cinematic, match colour", "status": "supported",
        "commands": ["lumetri.presets", "lumetri.applyPreset {name}", "lumetri.applyMatch {clip,referenceTime}",
                     "effects.setParam on the lumetri effect"],
        "missing": "",
    },
    {
        "id": "qc", "name": "Find problems (read-only report)", "status": "partly",
        "commands": ["sequence_inspect (gaps)", "clip.audioPeak (clipping)", "project_inspect (frame rates)",
                     "media.status (offline)"],
        "missing": "black/flash-frame detection. Loudness: there is no read-only LUFS meter; "
                   "essentialSound.autoMatch reports the LUFS it measured but also changes the clip's gain "
                   "(undo it afterwards if the user only wanted the number)",
    },
    {
        "id": "notes", "name": "Client notes → markers/edits", "status": "supported",
        "commands": ["markers.add {time|seconds,name,comment}", "timeline.move / sequence edits for clear notes"],
        "missing": "",
    },
]


# ROUND 2b: a real editor's chores.
FEATURES += [
    {
        "id": "check-material", "name": "Check material (what's in each clip, find a take by words)", "status": "partly",
        "commands": ["transcript.generate (or sequence.transcribe; a long clip can take a minute and the call may "
                     "time out while it finishes: check with transcript.inspect)",
                     "transcript.inspect {paragraphGapSeconds:2} (readable paragraphs, not word lists)",
                     "transcript.search {query}",
                     "clip.sceneEditDetection {applyCuts|generateMarkers}", "render_frame (look at frames)"],
        "missing": "black/flash-frame detection; visual look-through only as single rendered frames",
        "needs": "speech-to-text (FilmCraft built with its `whisper` feature + a speech model)",
    },
    {
        "id": "text-cuts", "name": "Text-based cuts", "status": "supported",
        "commands": ["transcript.select/lift/extract {from,to}", "transcript.removeFillers", "transcript.removePauses",
                     "clip.sceneEditDetection"],
        "missing": "",
        "needs": "speech-to-text (see above)",
    },
    {
        "id": "blur-track", "name": "Blur something across the video (privacy)", "status": "partly",
        "commands": ["effects.apply {effect:gaussian_blur}", "effects.setParam (blurriness)",
                     "masks.add {effect:<the blur>,shape:rectangle|ellipse,center,size}", "masks.set {feather,trackMethod}",
                     "masks.track {direction,method,wait:true}", "masks.list {time} (check the tracked path)"],
        "missing": "face/object detection: FilmCraft can't find what to blur. The user (or the AI from a described "
                   "region or a rendered frame) places the box once; the AI adds the blur and tracks the mask",
    },
    {
        "id": "transitions", "name": "Transitions between clips", "status": "supported",
        "commands": ["sequence.applyVideoTransition {clip,effect:cross_dissolve,frames,edge,align}",
                     "sequence.setTransition", "sequence.removeTransition", "effects.list {kind:VideoTransition}"],
        "missing": "",
    },
    {
        "id": "audio-repair", "name": "Noise reduction and loudness", "status": "supported",
        "commands": ["essentialSound.setType dialogue",
                     "essentialSound.set keys repair.noise.on/amount, repair.dehum.on/amount, repair.humHz, "
                     "repair.deess.on/amount, repair.reverb.on/amount, repair.rumble.on/amount",
                     "essentialSound.autoMatch {target:lufs}", "sequence.normalizeMixTrack", "essentialSound.generateDucking"],
        "missing": "a read-only loudness meter (autoMatch reports the measured LUFS while changing gain); "
                   "there is no audio.dialogueTargetLufs command (use autoMatch's target)",
    },
    {
        "id": "voiceover", "name": "Voice-over recording", "status": "partly",
        "commands": ["audio.voiceover.settings {source,inputChannel,countdownSoundCues,prerollSeconds}",
                     "audio.voiceover.start {track,time}", "audio.voiceover.stop"],
        "missing": "microphone input volume (that is a macOS setting, not FilmCraft's)",
        "needs": "the user's OK before recording from the microphone",
    },
]


def guide_section() -> str:
    lines = ["Feature map (real commands; do not fake what is missing):"]
    for f in FEATURES:
        line = f"- {f['name']}: {'; '.join(f['commands'])}."
        if f.get("missing"):
            line += f" NOT SUPPORTED by FilmCraft: {f['missing']}."
        if f.get("needs"):
            line += f" Needs {f['needs']}."
        lines.append(line)
    return "\n".join(lines)
