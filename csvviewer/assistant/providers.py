"""Assistant providers reached through the OpenAI-compatible chat API with the user's own (free) key."""
from dataclasses import dataclass, field

from PyQt6.QtCore import QSettings


@dataclass
class Provider:
    id: str  # 'gemini' / 'groq'; also the settings key prefix
    name: str  # shown in the picker and messages, e.g. "Google Gemini"
    company: str  # who rejects keys / sets limits, e.g. "Google"
    baseUrl: str  # OpenAI-compatible root, ends with '/'
    keyUrl: str  # where the user creates a free key
    keyPlaceholder: str
    models: list = field(default_factory=list)  # [(modelId, label)]
    defaultModel: str = ''
    signupText: str = ''  # signed-out explanation in the panel
    limitHint: str = ''  # appended to the "free limit used up" message
    reasoningModels: tuple = ()  # model id prefixes that accept reasoning_effort
    stripAdditionalProperties: bool = False  # Gemini's function schemas may reject it

    def settings(self):
        return QSettings('csv-viewer', 'csv-viewer')

    def savedKey(self):
        return self.settings().value(f'assistant/{self.id}Key', '') or ''

    def saveKey(self, key):
        self.settings().setValue(f'assistant/{self.id}Key', key.strip())

    def forgetKey(self):
        self.settings().remove(f'assistant/{self.id}Key')

    def savedModel(self):
        model = self.settings().value(f'assistant/{self.id}Model', '') or self.defaultModel
        # A model dropped from the list falls back to the default
        return model if model in dict(self.models) else self.defaultModel

    def saveModel(self, model):
        self.settings().setValue(f'assistant/{self.id}Model', model)

    def extraBody(self, model):
        """Request fields that only some models accept."""
        return {'reasoning_effort': 'low'} if model.startswith(self.reasoningModels) else {}


GEMINI = Provider(
    id='gemini', name='Google Gemini', company='Google',
    baseUrl='https://generativelanguage.googleapis.com/v1beta/openai/',
    keyUrl='https://aistudio.google.com/apikey', keyPlaceholder='Paste your Gemini API key',
    models=[('gemini-3.8-flash', 'Gemini 3.8 Flash'),
            ('gemini-3.5-flash-lite', 'Gemini 3.5 Flash-Lite (fastest, highest free limits)'),
            ('gemini-2.5-flash', 'Gemini 2.5 Flash')],
    defaultModel='gemini-3.8-flash',
    signupText='Free: create a Gemini API key with your Google account - no payment card. Free-tier limits apply, '
               'and Google may use free-tier prompts to improve its products.',
    limitHint='Wait a minute, or pick Gemini 3.5 Flash-Lite, which has higher free limits.',
    reasoningModels=('gemini',), stripAdditionalProperties=True)

GROQ = Provider(
    id='groq', name='Groq', company='Groq',
    baseUrl='https://api.groq.com/openai/v1/',
    keyUrl='https://console.groq.com/keys', keyPlaceholder='Paste your Groq API key (gsk_...)',
    models=[('openai/gpt-oss-120b', 'GPT-OSS 120B (best)'),
            ('openai/gpt-oss-20b', 'GPT-OSS 20B (faster)'),
            ('llama-3.3-70b-versatile', 'Llama 3.3 70B')],
    defaultModel='openai/gpt-oss-120b',
    signupText='Free: create a Groq API key (sign up with email, Google or GitHub) - no payment card. '
               'The free plan allows about 30 requests a minute and 1,000 a day, with a small per-minute token budget, '
               'so long questions may pause for a minute.',
    limitHint='The free plan has a small per-minute budget: wait a minute and ask again, or try GPT-OSS 20B.',
    reasoningModels=('openai/gpt-oss',))

KEY_PROVIDERS = {p.id: p for p in (GEMINI, GROQ)}
