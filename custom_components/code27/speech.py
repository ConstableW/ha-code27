"""CODE27 speech output without HASE bridge or logbook dependencies."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .api import Code27Error

_LOGGER = logging.getLogger(__name__)
_UTTERANCE_DONE = {"completed", "failed", "cancelled"}

async def async_speak_text(hass, entry, coordinator, text, entity_id=None, *, expected_slot=None):
    if coordinator.stopping or not coordinator.data:
        return
    slot = coordinator.data.current_slot.get("slot_number", 1)
    if expected_slot is not None:
        active = await coordinator.client.get_current_slot()
        if not isinstance(active, dict) or active.get("slot_number") != expected_slot or coordinator.stopping:
            return
        slot = expected_slot
    # Firmware 1.5.3: tts/utterances speaks in the active character's own
    # voice and reports completion. `speak_text` returns None on older
    # firmware, which falls through to the preview endpoint below.
    utterance = await coordinator.client.speak_text(text, slot)
    if isinstance(utterance, dict):
        coordinator.data.last_tts_message = {
            "content": text,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        coordinator.async_update_listeners()
        utterance_id = utterance.get("utterance_id")
        state = str(utterance.get("state") or "").lower()
        try:
            async with asyncio.timeout(60):
                while utterance_id and not coordinator.stopping and state not in _UTTERANCE_DONE:
                    await asyncio.sleep(0.35)
                    try:
                        state = str(
                            (await coordinator.client.get_utterance(str(utterance_id))).get("state") or ""
                        ).lower()
                    except Code27Error as err:  # a missed status poll does not undo accepted speech
                        _LOGGER.debug("CODE27 TTS status poll failed: %s", err)
        except TimeoutError:
            _LOGGER.debug("CODE27 TTS completion not confirmed before deadline")
        return
    settings = next(
        (item for item in coordinator.data.character_slots if item.get("slot_number") == slot), {}
    )
    voice_id = settings.get("voice_id")
    if not voice_id:
        return
    await coordinator.client.tts_audition(voice_id, text, settings.get("group_key") or "")
    coordinator.data.last_tts_message = {
        "content": text,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    coordinator.async_update_listeners()
    try:
        async with asyncio.timeout(15):
            while not coordinator.stopping:
                await asyncio.sleep(0.35)
                try:
                    state = (await coordinator.client.get_tts_audition_state()).get("state")
                except Code27Error as err:  # a missed status poll does not undo accepted speech
                    _LOGGER.debug("CODE27 TTS status poll failed: %s", err)
                    continue
                if (state or "").upper() in {"ENDED", "FAILED"}:
                    return
    except TimeoutError:
        _LOGGER.debug("CODE27 TTS completion not confirmed before deadline")
