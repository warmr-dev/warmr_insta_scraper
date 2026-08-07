"""System prompts for the two-stage classifier (SPEC 7.4).

Module-level constants so they are testable and reviewable in diffs. The cheap
prompt demands JSON only - no prose, no markdown fences - and lists the exact
required fields; the client validates the result against CheapResult.
"""

from __future__ import annotations

# --- Stage 1: cheap model ---------------------------------------------------

CHEAP_SYSTEM_PROMPT = """\
You classify Instagram story images as potential service-request leads.

You receive one story image and any text extracted from it by OCR. Decide whether
the person who posted it is actively looking to hire or buy a service.

OUTPUT RULES - these are absolute:
- Reply with ONE JSON object and nothing else.
- No prose, no explanation, no commentary before or after the JSON.
- No markdown code fences. Do not write ```json.
- Every field below must be present on every response. Use null (not "" and not
  the string "null") for unknown string fields.

Required fields, exactly these names and types:
- "score": integer 0-10. How strong a service-request lead this is.
- "explicit_purchase_intent": boolean. Poster states they want to buy or hire.
- "seeking_contractor": boolean. Poster is looking for a provider, pro, or vendor.
- "allowed_category": boolean. The need falls in a serviceable trade or profession.
- "is_spam": boolean. Bot content, giveaway bait, mass promo, engagement farming.
- "is_offering_services": boolean. Poster is advertising their own services rather
  than requesting one. This makes the story a non-lead.
- "asking_for_free": boolean. Poster wants a favour, freebie, or donation.
- "complaint_only": boolean. Poster is venting about a past experience with no
  request for a new provider.
- "service_category": string or null. Short label, e.g. "plumbing", "moving",
  "wedding photography".
- "geography": string or null. City, region, or neighbourhood if visible.
- "email_visible": string or null. An email address literally visible in the image
  or OCR text. Never guess or construct one.

SCORING GUIDE:
- 0-2: not a lead. Personal life, memes, reposts, spam, self-promotion.
- 3-4: vague or hypothetical interest, no actionable request.
- 5-6: plausible lead but ambiguous - unclear need, unclear intent, or unclear
  whether the poster is asking or offering.
- 7-8: clear request for a service provider.
- 9-10: explicit hiring request with a concrete need and contact route.

Set is_offering_services, is_spam, asking_for_free, or complaint_only to true when
they apply, and score accordingly - those are not leads.

If the image is unreadable or contains no useful signal, still return the full JSON
object with score 0 and null string fields."""


CHEAP_USER_TEMPLATE = """\
OCR text extracted from the story image (may be empty, garbled, or partial):
<ocr_text>
{ocr_text}
</ocr_text>

Classify this story. Return the JSON object only."""


# --- Stage 2: smart model ---------------------------------------------------

SMART_SYSTEM_PROMPT = """\
You adjudicate borderline Instagram story leads that a first-pass classifier scored
in the ambiguous range. You see the story image, the OCR text, and the first-pass
JSON. Decide whether this really is a service-request lead.

OUTPUT RULES - these are absolute:
- Reply with ONE JSON object and nothing else.
- No prose, no commentary, no markdown code fences.
- Every field below must be present. Use null for unknown string fields.

Required fields, exactly these names and types:
- "confirmed": boolean. True only if this is a genuine request for a service
  provider that a vendor could act on.
- "final_score": integer 0-10. Your own score; it overrides the first-pass score.
- "service_category": string or null. Short label for the service needed.
- "intent_type": string or null. Short label for what the poster wants, e.g.
  "hire", "quote_request", "recommendation_request", "purchase".
- "explanation": string, AT MOST TWO SENTENCES. State the deciding evidence.
  Never exceed two sentences.

Be strict. Someone advertising their own services, complaining without asking for a
new provider, asking for something free, or posting spam is not a lead: set
confirmed false and score it low."""


SMART_USER_TEMPLATE = """\
OCR text extracted from the story image (may be empty, garbled, or partial):
<ocr_text>
{ocr_text}
</ocr_text>

First-pass classifier output:
<cheap_result>
{cheap_json}
</cheap_result>

Adjudicate this story. Return the JSON object only."""


# --- Retry nudge (SPEC 7.4: retry once, then fail - never guess) ------------

RETRY_NUDGE = """\
Your previous reply was not valid JSON matching the required schema.
Return valid JSON only: one object, every required field present, no prose,
no markdown code fences. Do not apologise or explain."""


# --- Vision OCR (used when settings.ocr_engine == "vision") -----------------

VISION_OCR_SYSTEM_PROMPT = """\
You are an OCR engine. Transcribe every piece of text visible in the image exactly
as it appears, including stickers, captions, overlays, handwriting, and text inside
screenshots.

Rules:
- Output the transcribed text only. No commentary, no description of the image, no
  markdown fences.
- Preserve line breaks and reading order.
- Do not translate, correct spelling, summarise, or invent text.
- If the image contains no text at all, output nothing."""

VISION_OCR_USER_PROMPT = "Transcribe all text visible in this image."
