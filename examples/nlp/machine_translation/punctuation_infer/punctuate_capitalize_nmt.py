import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple, Union

import torch

from nemo.collections.nlp.models.machine_translation import MTEncDecModel
from nemo.collections.nlp.modules.common.transformer import BeamSearchSequenceGenerator


def get_args():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="The script is for restoring punctuation and capitalization in text. Long strings are split into "
        "segments of length `--max_seq_length`. `--max_seq_length` is the length which includes [CLS] and [SEP] "
        "tokens. Parameter `--step` controls segments overlapping. `--step` is a distance between beginnings of "
        "consequent segments. Model outputs for tokens near the borders of tensors are less accurate and can be "
        "discarded before final predictions computation. Parameter `--margin` is number of discarded outputs near "
        "segments borders. If model predictions in overlapping parts of segments are different most frequent "
        "predictions is chosen.",
    )
    input_ = parser.add_mutually_exclusive_group(required=True)
    input_.add_argument(
        "--input_manifest",
        "-m",
        type=Path,
        help="Path to the file with NeMo manifest which needs punctuation and capitalization. If the first element "
        "of manifest contains key 'pred_text', 'pred_text' values are passed for tokenization. Otherwise 'text' "
        "values are passed for punctuation and capitalization. Exactly one parameter of `--input_manifest` and "
        "`--input_text` should be provided.",
    )
    input_.add_argument(
        "--input_text",
        "-t",
        type=Path,
        help="Path to file with text which needs punctuation and capitalization. Exactly one parameter of "
        "`--input_manifest` and `--input_text` should be provided.",
    )
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument(
        "--output_manifest",
        "-M",
        type=Path,
        help="Path to output NeMo manifest. Text with restored punctuation and capitalization will be saved in "
        "'pred_text' elements if 'pred_text' key is present in the input manifest. Otherwise text with restored "
        "punctuation and capitalization will be saved in 'text' elements. Exactly one parameter of `--output_manifest` "
        "and `--output_text` should be provided.",
    )
    output.add_argument(
        "--output_text",
        "-T",
        type=Path,
        help="Path to file with text with restored punctuation and capitalization. Exactly one parameter of "
        "`--output_manifest` and `--output_text` should be provided.",
    )
    parser.add_argument(
        "--model_path",
        "-P",
        type=Path,
        help=f"Path to .nemo checkpoint of `MTEncDecModel`. No more than one of parameters ",
    )
    parser.add_argument(
        "--max_seq_length",
        "-L",
        type=int,
        default=64,
        help="Numbers of words in segments into which queries are split.",
    )
    parser.add_argument(
        "--step",
        "-s",
        type=int,
        default=8,
        help="Number of words between beginnings of consequent segments."
    )
    parser.add_argument(
        "--margin",
        "-g",
        type=int,
        default=16,
        help="A number of words near borders in segments which are not used for punctuation and capitalization "
        "prediction.",
    )
    parser.add_argument(
        "--batch_size", "-b", type=int, default=128, help="Number of segments which are processed simultaneously.",
    )
    parser.add_argument(
        "--device",
        "-d",
        choices=['cpu', 'cuda'],
        help="Which device to use. If device is not set and CUDA is available, then GPU will be used. If device is "
        "not set and CUDA is not available, then CPU is used.",
    )
    parser.add_argument(
        "--add_source_num_words_to_batch",
        action="store_true",
        help="Whether to pass number of words in source sequences to beam search generator. Set this if fixed length " \
        "beam search is used."
    )
    parser.add_argument(
        "--capitalization_labels",
        defalt="OuU",
        help="A string containing all characters used as capitalization labels. THE FIRST CHARACTER IN A STRING HAS "
        "TO BE NEUTRAL LABEL."
    )
    args = parser.parse_args()
    if args.input_manifest is None and args.output_manifest is not None:
        parser.error("--output_manifest requires --input_manifest")
    if args.max_seq_length <= 0:
        parser.error(
            f"Parameter `--max_seq_length` has to be positive, whereas `--max_seq_length={args.max_seq_length}`"
        )
    if args.max_seq_length - 2 * args.margin < args.step:
        parser.error(
            f"Parameters `--max_seq_length`, `--margin`, `--step` must satisfy condition "
            f"`max_seq_length - 2 * margin >= step` whereas `--max_seq_length={args.max_seq_length}`, "
            f"`--margin={args.margin}`, `--step={args.step}`."
        )
    for name in ["input_manifest", "input_text", "output_manifest", "output_text", "model_path"]:
        if getattr(args, name) is not None:
            setattr(args, name, getattr(args, name).expanduser())
    return args


def load_manifest(manifest: Path) -> List[Dict[str, Union[str, float]]]:
    result = []
    with manifest.open() as f:
        for i, line in enumerate(f):
            data = json.loads(line)
            result.append(data)
    return result


def split_into_segments(texts: List[str], max_seq_length: int, step: int) -> Tuple[List[str], List[int], List[int]]:
    segments, query_indices, start_word_i = [], [], []
    segment_start = 0
    for q_i, query in enumerate(texts):
        words = query.split()
        while segment_start + max_seq_length < len(words):
            segments.append(' '.join(words[segment_start : segment_start + max_seq_length]))
            start_word_i.append(segment_start)
            query_indices.append(q_i)
            segment_start += step
    return segments, query_indices, start_word_i


def adjust_predicted_labels_length(
    segments: List[str], autoregressive_labels: List[str], capitalization_labels: str
) -> List[str]:
    result = []
    capitalization_pattern = re.compile(f"[{capitalization_labels}]")
    for i, (segment, labels) in enumerate(zip(segments, autoregressive_labels)):
        num_words = len(segment.split())
        num_word_labels = len(capitalization_pattern.findall(segment))
        if num_words > num_word_labels:
            if labels[-1] != ' ':
                labels += ' ' + (capitalization_labels[0] + ' ') * (num_words - num_word_labels)
        elif num_words < num_word_labels:
            i = num_word_labels
            pos = len(labels) - 1
            while i > num_words:
                if labels[pos] in capitalization_labels:
                    i -= 1
                pos -= 1
            labels = labels[: pos + 1]
        result.append(labels)
    return result


def apply_autoregressive_labels(
    queries: List[str],
    segment_autoregressive_labels: List[str],
    query_indices: List[int],
    start_word_i: List[int],
    step: int,
    margin: int,
    capitalization_labels: str,
) -> List[str]:
    capitalization_pattern = re.compile(f"([{capitalization_labels}])")
    result = []
    current_segment_i = 0
    for q_i, query in enumerate(queries):
        words = query.split()
        num_words = len(words)
        punctuation_voting = [Counter() for _ in range(num_words + 1)]
        capitalization_voting = [Counter() for _ in range(num_words)]
        j = 0
        while query_indices[j] == q_i:
            num_words_in_segment = len(capitalization_pattern.findall(segment_autoregressive_labels[j]))
            the_last_segment = j * step + num_words_in_segment >= num_words
            labels = capitalization_labels.split(segment_autoregressive_labels[j])
            num_processed = 0
            for lbl_i, lbl in enumerate(labels):
                if lbl in capitalization_labels:
                    num_processed += 1
                if j > 0 and num_processed <= margin != 0:
                    continue
                if not the_last_segment and num_processed > num_words_in_segment - margin:
                    break
                if lbl_i % 2:
                    assert lbl in capitalization_labels
                    if lbl:
                        capitalization_voting[lbl_i // 2].update([lbl])
                else:
                    assert lbl not in capitalization_labels
                    if lbl:
                        punctuation_voting[lbl_i // 2].update([lbl])




def main():
    args = get_args()
    if args.pretrained_name is None:
        model = MTEncDecModel.restore_from(args.model_path)
    else:
        model = MTEncDecModel.from_pretrained(args.pretrained_name)
    if args.device is None:
        if torch.cuda.is_available():
            model = model.cuda()
        else:
            model = model.cpu()
    else:
        model = model.to(args.device)
    if args.input_manifest is None:
        texts = []
        with args.input_text.open() as f:
            for line in f:
                texts.append(line.strip())
    else:
        manifest = load_manifest(args.input_manifest)
        text_key = "pred_text" if "pred_text" in manifest[0] else "text"
        texts = []
        for item in manifest:
            texts.append(item[text_key])
    segments, query_indices, start_word_i = split_into_segments(texts, args.max_seq_length, args.margin)
    model.beam_search = BeamSearchSequenceGenerator(
        embedding=model.decoder.embedding,
        decoder=model.decoder.decoder,
        log_softmax=model.log_softmax,
        bos=model.decoder_tokenizer.bos_id,
        pad=model.decoder_tokenizer.pad_id,
        eos=model.decoder_tokenizer.eos_id,
        max_sequence_length=model.decoder.max_sequence_length,
        beam_size=args.beam_size,
        len_pen=args.len_pen,
        max_delta_length=args.max_delta_length,
        decoder_word_ids=model.decoder_tokenizer.word_ids,
    )
    autoregressive_punctuation_labels = []
    for i in range(0, len(segments), args.batch_size):
        autoregressive_punctuation_labels += model.translate(
            text=segments[i : i + args.batch_size],
            source_lang=args.source_lang,
            target_lang=args.target_lang,
            return_beam_scores=args.write_scores,
            log_timing=args.write_timing,
            add_src_num_words_to_batch=args.add_src_num_words_to_batch,
        )
    processed_texts = apply_autoregressive_labels(
        texts, autoregressive_punctuation_labels, query_indices, start_word_i, args.step, args.margin, args.capitalization_labels
    )
    if args.output_manifest is None:
        args.output_text.parent.mkdir(exist_ok=True, parents=True)
        with args.output_text.open('w') as f:
            for t in processed_texts:
                f.write(t + '\n')
    else:
        args.output_manifest.parent.mkdir(exist_ok=True, parents=True)
        with args.output_manifest.open('w') as f:
            for item, t in zip(manifest, processed_texts):
                item[text_key] = t
                f.write(json.dumps(item) + '\n')


if __name__ == "__main__":
    main()