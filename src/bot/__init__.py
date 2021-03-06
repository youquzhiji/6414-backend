import io
import os
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import matplotlib
import parselmouth
import sgs
import tensorflow_io as tfio
from inaSpeechSegmenter import Segmenter
from inaSpeechSegmenter.constants import ResultFrame
from inaSpeechSegmenter.features import to_wav
from inaSpeechSegmenter.sidekit_mfcc import read_wav
from telegram import Update, Message, Bot
from telegram.ext import Updater, CallbackContext, Dispatcher, CommandHandler, MessageHandler, \
    Filters

sys.path.append(str(Path(__file__).parent))
sys.path.append(str(Path(__file__).parent.parent))

from bot import utils, web
from bot.render import draw_ml, draw_mspect


@dataclass
class AnalyzeComponents:
    """
    Defines which aspects of the voice should the bot analyze.
    """
    ml: bool
    spect: bool
    stats: bool

    @classmethod
    def from_command(cls, cmd: str) -> "AnalyzeComponents":
        out = cls(False, False, False)

        cmd = cmd.lower().strip()
        full = cmd == 'analyze'

        out.ml = full or cmd in ['ml']
        out.spect = full or cmd in ['spectrogram', 'formant', 'pitch']
        out.stats = full or cmd in ['stats']

        return out


def segment(file) -> list[ResultFrame]:
    return [ResultFrame(*s) for s in seg(file)]


def r(u: Update, msg: str, md=True):
    updater.bot.sendMessage(chat_id=u.effective_chat.id, text=msg,
                            parse_mode='Markdown' if md else None)


def cmd_start(u: Update, c: CallbackContext):
    r(u, '欢迎! 点下面的录音按钮就可以开始啦w')


def cmd_ml(file: Path, msg: Message):
    # Segment file
    result = segment(file)

    # Null case
    print(result)
    assert len(result), '分析失败, 大概是音量太小或者时长太短吧, 再试试w'

    # Draw results
    with draw_ml(str(file), result) as buf:
        f, m, o, pf = get_result_percentages(result)
        send = f"CNN 模型分析结果: {f*100:.0f}% 🙋‍♀️ | {m*100:.0f}% 🙋‍♂️ | {o*100:.0f}% 🚫\n" \
               f"(结果仅供参考, 如果结果不是你想要的，那就是模型的问题，欢迎反馈)\n"
        bot.send_photo(msg.chat_id, photo=buf, caption=send,
                       reply_to_message_id=msg.message_id)


def cmd_spect(file: Path, msg: Message):
    # Read file
    wav_full = to_wav(file, sr=None)
    y, sr, _ = read_wav(wav_full)
    sound = parselmouth.Sound(y, sr)

    t = tfio.audio.spectrogram(y, 2048, 2048, 256)
    mel_spectrogram = tfio.audio.melscale(t, rate=sr, mels=128, fmin=0, fmax=8000)

    result, freq_array = sgs.api.calculate_feature_classification(sound)

    mspec = draw_mspect(mel_spectrogram, freq_array, sr)
    buf = io.BytesIO()
    mspec.save(buf, 'JPEG')
    buf.seek(0)

    send = f'显示基频和共振峰的频谱图\n' \
           f'（目前用了 Praat 算法，希望以后能改成 DeepFormants）'
    bot.send_document(msg.chat_id, document=buf, filename='spectrogram.jpg', caption=send)


def process_audio(cmd: str, msg: Message):
    audio = msg.audio or msg.voice
    assert audio

    # Download audio file
    date = datetime.now().strftime('%Y-%m-%d %H-%M')
    downloader = bot.getFile(audio.file_id)
    file = Path(tmpdir).joinpath(f'{date} {msg.from_user.name[1:]}.mp3')
    print(downloader, '->', file)
    downloader.download(file)

    # Command flags
    flags = AnalyzeComponents.from_command(cmd)

    if flags.ml:
        cmd_ml(file, msg)
    if flags.spect:
        cmd_spect(file, msg)
    # if flags.stats:
    #     raise AssertionError('Stats 功能还没有实现')


def cmd_reply(u: Update, c: CallbackContext):
    try:
        reply = u.effective_message.reply_to_message

        # Parse command (No error if this is not a command for the bot)
        text = u.effective_message.text
        assert text

        cmd = text.lower().split()[0].strip()
        assert cmd[0] in '!/'

        cmd = cmd[1:]
        assert cmd in ['analyze', 'ml', 'formant', 'pitch', 'stats']

        # Parse Audio (No error if the replied message doesn't contain audio)
        audio = reply.audio or reply.voice
        assert audio

        # Check replying to oneself
        assert u.effective_user.id == reply.from_user.id, '只有自己能分析自己的音频哦 👀'

        process_audio(cmd, reply)

    except AssertionError as e:
        if str(e) != '':
            r(u, str(e))


def on_audio(u: Update, c: CallbackContext):
    try:
        process_audio('analyze', u.effective_message)
    except AssertionError as e:
        if str(e) != '':
            r(u, str(e))


def get_result_percentages(result: list[ResultFrame]) -> tuple[float, float, float, float]:
    """
    Get percentages

    :param result: Result
    :return: %female, %male, %other, %female-vs-female+male
    """
    # Count total and categorical durations
    total_dur = 0
    durations: dict[str, int] = {f.label: 0 for f in result}
    for f in result:
        dur = f.end - f.start
        durations[f.label] += dur
        total_dur += dur

    # Convert durations to ratios
    for d in durations:
        durations[d] /= total_dur

    # Return results
    f = durations.get('female', 0)
    m = durations.get('male', 0)

    fm_total = f + m
    pf = 0 if fm_total == 0 else f / fm_total

    return f, m, 1 - f - m, pf


if __name__ == '__main__':
    utils.init_tf()

    warnings.filterwarnings("ignore")
    matplotlib.use('agg')

    seg = Segmenter()

    tmpdir = Path('audio_tmp')
    tmpdir.mkdir(exist_ok=True, parents=True)

    # Find telegram token
    # path = Path(os.path.abspath(__file__)).parent.parent.parent
    if 'tg_token' in os.environ:
        tg_token = os.environ['tg_token']
    else:
        tg_token = Path('voice-bot-token.txt').read_text('utf-8').strip()

    # Start web server
    web_thread = web.start_async()

    # Start bot
    updater = Updater(token=tg_token, use_context=True)
    dispatcher: Dispatcher = updater.dispatcher
    bot: Bot = updater.bot

    dispatcher.add_handler(CommandHandler('start', cmd_start, filters=Filters.chat_type.private))
    dispatcher.add_handler(CommandHandler('analyze', cmd_reply, filters=Filters.reply))
    dispatcher.add_handler(MessageHandler(Filters.reply, cmd_reply))
    dispatcher.add_handler(MessageHandler(Filters.voice & Filters.chat_type.private, on_audio))
    dispatcher.add_handler(MessageHandler(Filters.audio & Filters.chat_type.private, on_audio))

    print('Bot started.')
    updater.start_polling()
