"""Sarvam translation with exact preservation of money, numbers and commands."""
import re
from concurrent.futures import ThreadPoolExecutor
import unicodedata

from intelligence import SarvamError, sarvam

LANGUAGES = {
    'en-IN': ('English', 'English'), 'ta-IN': ('தமிழ்', 'Tamil'), 'hi-IN': ('हिन्दी', 'Hindi'),
    'te-IN': ('తెలుగు', 'Telugu'), 'kn-IN': ('ಕನ್ನಡ', 'Kannada'), 'ml-IN': ('മലയാളം', 'Malayalam'),
    'bn-IN': ('বাংলা', 'Bengali'), 'mr-IN': ('मराठी', 'Marathi'), 'gu-IN': ('ગુજરાતી', 'Gujarati'),
    'pa-IN': ('ਪੰਜਾਬੀ', 'Punjabi'), 'od-IN': ('ଓଡ଼ିଆ', 'Odia'),
}
LANGUAGE_PROMPT = 'Choose your language / மொழியைத் தேர்ந்தெடுக்கவும் / अपनी भाषा चुनें:\n' + '\n'.join(
    f'{index}. {native} ({name})' for index, (native, name) in enumerate(LANGUAGES.values(), 1))
LANGUAGE_KEYBOARD = {'keyboard': [[LANGUAGES[k][0] for k in list(LANGUAGES)[i:i+3]]
                                for i in range(0, len(LANGUAGES), 3)], 'resize_keyboard': True, 'one_time_keyboard': True}
NAV_KEYBOARD = {'keyboard': [['/menu', '/continue'], ['/back', '/language']], 'resize_keyboard': True}
CONSENT_KEYBOARD = {'keyboard': [['/yes', '/no']], 'resize_keyboard': True, 'one_time_keyboard': True}

# Native shortcuts work offline; slash commands work in every language.
ALIASES = {
    '/yes': 'yes', '/no': 'no', 'back': '/back', 'go back': '/back', 'menu': '/menu',
    'continue': '/continue', 'resume': '/continue', '/resume': '/continue',
    'continue where left': '/continue', 'continue where i left': '/continue',
    'continue where i left off': '/continue', 'language': '/language', 'change language': '/language',
    'schemes': '/schemes', 'funding': '/schemes', 'documents': '/documents', 'details': '/details',
    'more': '/more', 'cost': '/cost', 'costs': '/cost', 'status': '/status',
    'பின்': '/back', 'பின்னால்': '/back', 'மெனு': '/menu', 'பட்டி': '/menu', 'தொடரு': '/continue',
    'தொடரவும்': '/continue', 'மொழி': '/language', 'உதவி': '/worker', 'ஆம்': 'yes', 'இல்லை': 'no', 'நிறுத்து': '/stop',
    'वापस': '/back', 'पीछे': '/back', 'मेनू': '/menu', 'जारी': '/continue', 'जारी रखें': '/continue',
    'भाषा': '/language', 'मदद': '/worker', 'हाँ': 'yes', 'हां': 'yes', 'नहीं': 'no', 'रोकें': '/stop',
    'వెనక్కి': '/back', 'కొనసాగించు': '/continue', 'మెను': '/menu', 'భాష': '/language', 'అవును': 'yes', 'కాదు': 'no',
    'ಹಿಂದೆ': '/back', 'ಮುಂದುವರಿಸಿ': '/continue', 'ಮೆನು': '/menu', 'ಭಾಷೆ': '/language', 'ಹೌದು': 'yes', 'ಇಲ್ಲ': 'no',
    'പിന്നോട്ട്': '/back', 'തുടരുക': '/continue', 'മെനു': '/menu', 'ഭാഷ': '/language', 'അതെ': 'yes', 'ഇല്ല': 'no',
    'ফিরে যান': '/back', 'চালিয়ে যান': '/continue', 'মেনু': '/menu', 'ভাষা': '/language', 'হ্যাঁ': 'yes', 'না': 'no',
    'मागे': '/back', 'पुढे चालू ठेवा': '/continue', 'मेन्यू': '/menu', 'होय': 'yes', 'नाही': 'no',
    'પાછા': '/back', 'ચાલુ રાખો': '/continue', 'મેનુ': '/menu', 'ભાષા': '/language', 'હા': 'yes', 'ના': 'no',
    'ਪਿੱਛੇ': '/back', 'ਜਾਰੀ ਰੱਖੋ': '/continue', 'ਮੀਨੂ': '/menu', 'ਭਾਸ਼ਾ': '/language', 'ਹਾਂ': 'yes', 'ਨਹੀਂ': 'no',
    'ପଛକୁ': '/back', 'ଜାରି ରଖନ୍ତୁ': '/continue', 'ମେନୁ': '/menu', 'ଭାଷା': '/language', 'ହଁ': 'yes', 'ନା': 'no',
}


def normalize_text(value):
    return ''.join(str(unicodedata.decimal(c)) if c.isdecimal() else c for c in value).strip()


def command_for(value):
    command = normalize_text(value).casefold().strip('.! ')
    if command.startswith('/'):
        command = re.sub(r'@\w+', '', command)
    return ALIASES.get(command, command)


def language_choice(text, numbered=False):
    clean = normalize_text(text).casefold()
    if numbered and clean.isascii() and clean.isdecimal() and 1 <= int(clean) <= len(LANGUAGES):
        return list(LANGUAGES)[int(clean)-1]
    for code, names in LANGUAGES.items():
        if clean in {code.casefold(), *(name.casefold() for name in names)}:
            return code
    return None


PROTECTED = re.compile(r'https?://[^\s]+|/[a-zA-Z_]+|₹[\d,]+(?:\.\d+)?|\d+(?:[.,]\d+)*(?:%)?')
STATIC_WORDS = {
    'Estimated treatment:', 'May pay up to:', 'May pay:', 'You may need:',
    'If eligible and approved.', 'resumes.', 'Support options:', 'HMCPF cancer fund', 'PM-JAY', 'CMCHIS',
    'Share my phone number', 'Document read.', 'File received.',
    'These are estimates if eligible and approved. Actual support may be lower. Compare one scheme at a time.',
    'The calculations use the higher treatment estimate.', 'Reply with a scheme number to choose.',
    'NUMBER explains a scheme.', 'shows more options.', 'goes back.',
    'Support amount: needs worker review.', 'Scheme support: needs worker review.',
    'resumes your application.', 'compares support.',
}
# Only explicitly listed public UI phrases are cached; patient text is never cached here.
_static_cache = {}


def translate_chunk(text, code):
    # Numeric values and commands stay outside the model, in their original positions.
    # Translation receives only the words between them, one line at a time.
    pieces=[]
    words=[]
    for line_no,line in enumerate(text.split('\n')):
        if line_no:
            pieces.append(('literal','\n'))
        position=0
        for match in PROTECTED.finditer(line):
            if match.start()>position:
                fragment=line[position:match.start()]
                if re.search('[A-Za-z]',fragment):
                    pieces.append(('word',len(words))); words.append(fragment)
                else:
                    pieces.append(('literal',fragment))
            pieces.append(('literal',match.group()))
            position=match.end()
        fragment=line[position:]
        if re.search('[A-Za-z]',fragment):
            pieces.append(('word',len(words))); words.append(fragment)
        else:
            pieces.append(('literal',fragment))
    if not words:
        return text
    missing=list(dict.fromkeys(w.strip() for w in words if (code,w.strip()) not in _static_cache))
    def translate_word(word):
        result=sarvam('POST','/translate',json={'input':word,'source_language_code':'en-IN',
                     'target_language_code':code,'model':'mayura:v1','mode':'formal','numerals_format':'international'})
        value=result.get('translated_text')
        if not isinstance(value,str) or not value.strip() or PROTECTED.search(value) or '₹' in value:
            raise SarvamError('Translation could not preserve the original values.')
        return value
    try:
        # Bounded concurrency keeps short replies responsive without sending amounts to the provider.
        with ThreadPoolExecutor(max_workers=3) as pool:
            translated=list(pool.map(translate_word,missing))
        resolved={word:_static_cache[(code,word)] for word in (w.strip() for w in words) if (code,word) in _static_cache}
        resolved.update(zip(missing,translated))
        output=[]
        for kind,value in pieces:
            if kind=='literal':
                output.append(value)
            else:
                original=words[value]
                lead=re.match(r'^\s*',original).group()
                tail=re.search(r'\s*$',original).group()
                output.append(lead+resolved[original.strip()].strip().replace('\n',' ')+tail)
        result=''.join(output)
        if PROTECTED.findall(result)!=PROTECTED.findall(text):
            raise ValueError()
        for word,value in zip(missing,translated):
            if word in STATIC_WORDS:
                _static_cache[(code,word)]=value
        return result
    except (KeyError,IndexError,TypeError,ValueError):
        raise SarvamError('Translation could not preserve the original values.') from None


def localize(text, code):
    if code in (None, 'en-IN') or code not in LANGUAGES or text == LANGUAGE_PROMPT:
        return text
    # Short replies usually need one call. Split on lines before masking expansion.
    chunks, current = [], ''
    for line in text.splitlines(keepends=True):
        if len(line) > 700:
            return 'English (translation temporarily unavailable):\n' + text
        if len(current) + len(line) > 700:
            chunks.append(current)
            current = ''
        current += line
    if current:
        chunks.append(current)
    try:
        return '\n'.join(translate_chunk(chunk, code) for chunk in chunks)
    except SarvamError:
        # Preserve financial figures and navigation when the provider is down or changes tokens.
        return 'English (translation temporarily unavailable):\n' + text
