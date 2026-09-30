import math
import re
import secrets


GAME_POOL_CHARACTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'


def normalize_game_answer(value: object) -> str:
	"""Return the canonical form used by the letter-board game."""
	return re.sub(r'[^A-Za-z0-9]', '', str(value or '')).upper()


def build_game_letter_pool(answer: object) -> tuple[str, list[str]]:
	normalized = normalize_game_answer(answer)
	if not normalized:
		return '', []

	letters = list(normalized)
	extra_count = max(3, min(6, math.ceil(len(normalized) / 2)))
	letters.extend(secrets.choice(GAME_POOL_CHARACTERS) for _ in range(extra_count))
	secrets.SystemRandom().shuffle(letters)
	return normalized, letters
