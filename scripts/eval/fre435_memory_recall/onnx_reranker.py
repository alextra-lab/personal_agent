"""FRE-697 — in-process ONNX cross-encoder reranker for the VPS-CPU separation benchmark.

FRE-695 established the cross-encoder reranker as the recall lever (best J=0.785) but showed the
local **llama.cpp** Qwen3-Reranker (causal yes/no-logit path) stalls under sustained load; MLX fixes
it, but only on the laptop. FRE-697 asks whether an **ONNX cross-encoder on the VPS CPU** — the
always-on, laptop-independent, fully-private path — reproduces that separation, and at what CPU
latency.

This module is the runtime-heavy half: a self-contained, dependency-injectable in-process scorer that
loads an ONNX cross-encoder (bge-reranker-v2-m3 or the Qwen3-Reranker-0.6B *sequence-classification*
head) and returns a relevance score per ``(query, document)`` pair. It touches NO substrate and imports
nothing from ``personal_agent`` — the harness (``separation_benchmark.py``) feeds it the FRE-670 probe
and reuses the FRE-694/695 separation metrics on the scores it returns.

Torch-free by design (FRE-697, owner-approved): ``onnxruntime`` for CPU inference + a ``transformers``
tokenizer only (numpy tensors; no torch import path). Optional dependency group ``onnx-eval`` — never in
the production gateway image. The ``onnxruntime`` session is thread-bounded (``intra_op=4``/``inter_op=1``
of 8 cores) so the benchmark leaves headroom for the live gateway on the shared VPS.

Correctness discipline (FRE-694/695): the Qwen3 seq-cls path replicates the model-card prompt template
**exactly** (a wrong template silently invalidates every score); ``load`` asserts the ONNX graph's input
names + output rank; ``verify_instrument`` runs the model card's own 4-document ranking example (Mars
must rank #1) before any aggregate is trusted.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

#: The Qwen3-Reranker seq-cls model-card system prefix (verbatim — do not edit).
_QWEN_SYSTEM_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query "
    'and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n'
    "<|im_start|>user\n"
)
#: The Qwen3-Reranker seq-cls model-card document suffix (verbatim — do not edit).
_QWEN_DOC_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
#: The model's *trained* instruction — kept native (not memory-phrased) so scoring stays on the
#: model's own distribution; the choice is documented in the FRE-697 research doc.
DEFAULT_QWEN_INSTRUCTION = (
    "Given a web search query, retrieve relevant passages that answer the query"
)

#: Model-card instrument-verification example: the true match (Mars) must rank #1 over near-topic
#: distractors (Venus/Jupiter/Saturn) — the polarity + template + tokenizer-wiring gate (FRE-695).
_VERIFY_QUERY = "Which planet is known as the Red Planet?"
_VERIFY_TRUE_MATCH = (
    "Mars, known for its reddish appearance, is often referred to as the Red Planet."
)
_VERIFY_DISTRACTORS = (
    "Venus is often called Earth's twin because of its similar size and proximity.",
    "Jupiter, the largest planet in our solar system, has a prominent red spot.",
    "Saturn, famous for its rings, is sometimes mistaken for the Red Planet.",
)


@dataclass(frozen=True)
class OnnxArm:
    """One ONNX reranker arm.

    Attributes:
        name: Arm id (the ``--arm`` value and JSON key).
        repo: Hugging Face repo the ONNX + tokenizer are pulled from.
        revision: Pinned commit sha (reproducibility — never a moving branch ref).
        onnx_file: Path within the repo to the ONNX graph (fp32 source when ``quantize``).
        family: Prompt/scoring family — ``"bge"`` (plain pair) or ``"qwen-seqcls"`` (template).
        quantize: Whether to dynamic-int8-quantize ``onnx_file`` at load (torch-free CPU int8).
        precision: Truthful precision label for the run record (e.g. ``"int8 (pre-exported)"``,
            ``"int8-dynamic"``, ``"fp32"``) — reflects the *actual* served graph, not merely whether
            this arm ran the quantizer.
        instruction: The seq-cls instruction (ignored by the ``bge`` family).
        engine: Human-readable engine label for the run record.
        max_length: Tokenizer max length; the document side is truncated, the query never.
    """

    name: str
    repo: str
    revision: str
    onnx_file: str
    family: str
    quantize: bool
    precision: str
    instruction: str
    engine: str
    max_length: int = 512


def format_pair(family: str, query: str, document: str, *, instruction: str) -> tuple[str, str]:
    """Build the ``(text_a, text_b)`` cross-encoder pair for one family.

    Args:
        family: ``"bge"`` (a plain query/document pair) or ``"qwen-seqcls"`` (the Qwen3-Reranker
            sequence-classification model-card template).
        query: The search query.
        document: The candidate document.
        instruction: The task instruction (used only by ``"qwen-seqcls"``).

    Returns:
        ``(text_a, text_b)`` to tokenize as a pair.

    Raises:
        ValueError: If ``family`` is unknown (never score with a wrong template).
    """
    if family == "bge":
        return query, document
    if family == "qwen-seqcls":
        text_a = f"{_QWEN_SYSTEM_PREFIX}<Instruct>: {instruction}\n<Query>: {query}\n"
        text_b = f"<Document>: {document}{_QWEN_DOC_SUFFIX}"
        return text_a, text_b
    raise ValueError(f"unknown reranker family {family!r} (expected 'bge' or 'qwen-seqcls')")


def logit_to_score(logit: float) -> float:
    """Numerically-stable logistic sigmoid of a relevance logit.

    Monotone, so it changes no separation/overlap/ranking verdict — only maps the arbitrary logit
    scale into a bounded ``[0, 1]`` score consistent with the harness's other arms.

    Args:
        logit: The relevance logit.

    Returns:
        ``sigmoid(logit)`` in ``[0, 1]``, overflow-safe on large-magnitude inputs.
    """
    if logit >= 0.0:
        z = np.exp(-logit)
        return float(1.0 / (1.0 + z))
    z = np.exp(logit)
    return float(z / (1.0 + z))


def squeeze_logits(raw: np.ndarray | Sequence[float] | Sequence[Sequence[float]]) -> list[float]:
    """Normalize an ONNX classifier output to one relevance logit per row.

    Handles the provider-dependent output shapes (FRE-697 codex review): a ``[batch]`` vector, a
    ``[batch, 1]`` single-logit head (bge, Qwen3 seq-cls), and a ``[batch, 2]`` two-class head (the
    positive-class log-odds ``col1 - col0``). Any other rank/width fails loud rather than scoring a
    misread tensor.

    Args:
        raw: The first ONNX output tensor.

    Returns:
        One relevance logit per input row.

    Raises:
        ValueError: If the tensor is not 1-D, ``[n, 1]``, or ``[n, 2]``.
    """
    arr = np.asarray(raw, dtype=np.float64)
    if arr.ndim == 1:
        return [float(x) for x in arr]
    if arr.ndim == 2:
        if arr.shape[1] == 1:
            return [float(x) for x in arr[:, 0]]
        if arr.shape[1] == 2:
            return [float(x) for x in (arr[:, 1] - arr[:, 0])]
        raise ValueError(
            f"unexpected logit column count {arr.shape[1]} (expected 1 or 2) — shape {arr.shape}"
        )
    raise ValueError(
        f"unexpected logit tensor rank {arr.ndim} (expected 1 or 2) — shape {arr.shape}"
    )


class _Session(Protocol):
    """The minimal ``onnxruntime.InferenceSession`` surface the scorer uses."""

    def run(self, output_names: object, input_feed: dict[str, np.ndarray]) -> list[np.ndarray]: ...


class _Tokenizer(Protocol):
    """The minimal ``transformers`` tokenizer surface the scorer uses (numpy tensors)."""

    def __call__(
        self, text_a: list[str], text_b: list[str], **kwargs: object
    ) -> dict[str, np.ndarray]: ...


def _sha256(path: Path) -> str:
    """SHA-256 of a file (provenance for the run record)."""
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cast_fp16_graph_to_fp32(src: Path, dst: Path) -> bool:
    """Cast a float16 ONNX graph to float32 in place (torch-free); no-op if already float32.

    ``onnxruntime.quantization.quantize_dynamic`` requires a **float32** source — fed a float16
    graph it emits float16 scale tensors the runtime then rejects (FRE-697). The only available
    seq-cls Qwen3-Reranker ONNX export ships float16, so this promotes it: float16 initializers →
    float32, ``Cast``-to-float16 nodes → float32, and float16 value-info/inputs/outputs → float32.
    Saved with external data so the ~2.4 GB float32 graph clears the 2 GB protobuf limit.

    Args:
        src: The source ONNX graph (float16 or float32).
        dst: Where to write the float32 graph.

    Returns:
        ``True`` if a float16→float32 cast was applied, ``False`` if the source was already float32
        (in which case ``dst`` is not written and the caller should use ``src`` directly).
    """
    import onnx  # noqa: PLC0415
    from onnx import TensorProto, numpy_helper  # noqa: PLC0415

    model = onnx.load(str(src))
    converted = False

    def _cast_tensor(tensor: TensorProto) -> bool:
        """Rewrite a float16 TensorProto to float32 in place; return whether it changed."""
        if tensor.data_type != TensorProto.FLOAT16:
            return False
        array = numpy_helper.to_array(tensor).astype(np.float32)
        tensor.CopyFrom(numpy_helper.from_array(array, tensor.name))
        return True

    for initializer in model.graph.initializer:
        converted |= _cast_tensor(initializer)
    for node in model.graph.node:
        # Cast-to-float16 nodes → float32.
        if node.op_type == "Cast":
            for attr in node.attribute:
                if attr.name == "to" and attr.i == TensorProto.FLOAT16:
                    attr.i = TensorProto.FLOAT
                    converted = True
        # Embedded tensors (Constant `value`, ConstantOfShape, etc.) also carry float16 weights —
        # missing them leaves a mixed-precision op (e.g. Mul(float16, float32)) the runtime rejects.
        for attr in node.attribute:
            if attr.HasField("t"):
                converted |= _cast_tensor(attr.t)
            for tensor in attr.tensors:
                converted |= _cast_tensor(tensor)
    for value in (*model.graph.value_info, *model.graph.input, *model.graph.output):
        if value.type.tensor_type.elem_type == TensorProto.FLOAT16:
            value.type.tensor_type.elem_type = TensorProto.FLOAT
            converted = True
    if not converted:
        return False
    onnx.save(
        model,
        str(dst),
        save_as_external_data=True,
        all_tensors_to_one_file=True,
        location=f"{dst.name}.data",
    )
    return True


class OnnxCrossEncoder:
    """In-process ONNX cross-encoder scorer (fail-loud, thread-bounded, DI-testable).

    Construct with an ``OnnxArm`` and either inject a ``session`` + ``tokenizer`` (unit tests) or call
    :meth:`load` to download + build them. :meth:`score` returns ``sigmoid`` relevance scores for a
    query against a candidate set, in input order.
    """

    def __init__(
        self,
        arm: OnnxArm,
        *,
        session: _Session | None = None,
        tokenizer: _Tokenizer | None = None,
        input_names: Sequence[str] | None = None,
        max_length: int | None = None,
    ) -> None:
        """Initialize the scorer.

        Args:
            arm: The arm definition (family, template, provenance).
            session: An injected inference session (skips :meth:`load`; used by tests).
            tokenizer: An injected tokenizer (skips :meth:`load`; used by tests).
            input_names: The ONNX graph input names to feed (defaults to ids + mask).
            max_length: Override the arm's tokenizer max length.
        """
        self.arm = arm
        self.family = arm.family
        self.instruction = arm.instruction
        self.max_length = max_length if max_length is not None else arm.max_length
        self._session = session
        self._tokenizer = tokenizer
        self.input_names: list[str] = (
            list(input_names) if input_names else ["input_ids", "attention_mask"]
        )
        #: Populated by :meth:`load`; surfaced in the run record.
        self.provenance: dict[str, object] = {}

    @property
    def session(self) -> _Session:
        """The inference session (raises if neither injected nor loaded)."""
        if self._session is None:
            raise RuntimeError("OnnxCrossEncoder.load() not called and no session injected")
        return self._session

    @property
    def tokenizer(self) -> _Tokenizer:
        """The tokenizer (raises if neither injected nor loaded)."""
        if self._tokenizer is None:
            raise RuntimeError("OnnxCrossEncoder.load() not called and no tokenizer injected")
        return self._tokenizer

    def load(self, *, cache_dir: Path, intra_op_threads: int = 4) -> None:
        """Download the ONNX graph + tokenizer at the pinned revision and build a CPU session.

        Optionally dynamic-int8-quantizes the graph (torch-free) into ``cache_dir``, bounds the
        onnxruntime CPU thread pools (``intra_op_threads``/``inter_op=1``) so the shared VPS keeps
        headroom for the live gateway, and asserts the graph's input names + output rank match what
        :meth:`score` feeds and reads. Records provenance (revision + fp32/int8 sha256 + config).

        Args:
            cache_dir: Directory for the quantized int8 artifact (gitignored).
            intra_op_threads: onnxruntime intra-op thread cap (of 8 VPS cores).

        Raises:
            RuntimeError: If the ONNX graph's inputs/outputs do not match the expected signature.
        """
        import onnxruntime as ort  # type: ignore[import-untyped]  # noqa: PLC0415
        from huggingface_hub import hf_hub_download  # noqa: PLC0415
        from transformers import AutoTokenizer  # noqa: PLC0415

        cache_dir.mkdir(parents=True, exist_ok=True)
        source_path = Path(
            hf_hub_download(self.arm.repo, self.arm.onnx_file, revision=self.arm.revision)
        )
        # External-data sibling (large graphs) rides alongside model.onnx — fetch if present.
        for sibling in (self.arm.onnx_file + "_data", self.arm.onnx_file + ".data"):
            try:
                hf_hub_download(self.arm.repo, sibling, revision=self.arm.revision)
            except Exception:  # noqa: BLE001 — sibling is optional; absence is the common case
                pass

        model_path = source_path
        int8_sha: str | None = None
        if self.arm.quantize:
            from onnxruntime.quantization import (  # type: ignore[import-untyped]  # noqa: PLC0415
                QuantType,
                quantize_dynamic,
            )

            # quantize_dynamic needs a float32 source; the seq-cls ONNX export ships float16, so
            # promote it first (idempotent for an already-float32 graph).
            quant_source = source_path
            fp32_path = cache_dir / f"{self.arm.name}.fp32.onnx"
            if not fp32_path.exists() and cast_fp16_graph_to_fp32(source_path, fp32_path):
                quant_source = fp32_path
            elif fp32_path.exists():
                quant_source = fp32_path
            int8_path = cache_dir / f"{self.arm.name}.int8.onnx"
            if not int8_path.exists():
                quantize_dynamic(str(quant_source), str(int8_path), weight_type=QuantType.QInt8)
            model_path = int8_path
            int8_sha = _sha256(int8_path)

        options = ort.SessionOptions()
        options.intra_op_num_threads = intra_op_threads
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        graph_inputs = {i.name for i in session.get_inputs()}
        self.input_names = [
            n for n in ("input_ids", "attention_mask", "token_type_ids") if n in graph_inputs
        ]
        if "input_ids" not in graph_inputs or "attention_mask" not in graph_inputs:
            raise RuntimeError(
                f"ONNX graph inputs {sorted(graph_inputs)} missing input_ids/attention_mask — "
                "wrong export?"
            )
        outputs = session.get_outputs()
        if not outputs:
            raise RuntimeError("ONNX graph exposes no outputs")

        self._session = session
        self._tokenizer = AutoTokenizer.from_pretrained(self.arm.repo, revision=self.arm.revision)
        self.provenance = {
            "repo": self.arm.repo,
            "revision": self.arm.revision,
            "onnx_file": self.arm.onnx_file,
            "precision": self.arm.precision,
            "source_sha256": _sha256(source_path),
            "int8_sha256": int8_sha,
            "intra_op_threads": intra_op_threads,
            "inter_op_threads": 1,
            "max_length": self.max_length,
            "input_names": self.input_names,
        }

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        """Relevance score of ``query`` against each document, in input order.

        Tokenizes the formatted pairs to numpy with the document side truncated to ``max_length``
        (the query side is never truncated), runs one forward pass, and maps the per-row relevance
        logit through :func:`logit_to_score`.

        Args:
            query: The search query.
            documents: The candidate documents.

        Returns:
            One ``sigmoid`` relevance score per document, in input order.

        Raises:
            ValueError: If the model returns a different number of rows than documents supplied.
        """
        if not documents:
            return []
        pairs = [
            format_pair(self.family, query, doc, instruction=self.instruction) for doc in documents
        ]
        text_a = [a for a, _ in pairs]
        text_b = [b for _, b in pairs]
        encoded = self.tokenizer(
            text_a,
            text_b,
            padding=True,
            truncation="only_second",
            max_length=self.max_length,
            return_tensors="np",
        )
        feed = {name: encoded[name] for name in self.input_names if name in encoded}
        outputs = self.session.run(None, feed)
        logits = squeeze_logits(outputs[0])
        if len(logits) != len(documents):
            raise ValueError(
                f"ONNX row-count mismatch: {len(logits)} logits != {len(documents)} documents"
            )
        return [logit_to_score(x) for x in logits]

    def verify_instrument(self) -> tuple[bool, float, float]:
        """Model-card ranking gate: the true match must rank #1 over near-topic distractors.

        Stronger than a single relevant/irrelevant pair (FRE-695 discipline): scores the Red-Planet
        example and checks Mars out-ranks Venus/Jupiter/Saturn. A pass confirms the prompt template,
        tokenizer wiring, and logit polarity are correct before any aggregate is trusted.

        Returns:
            ``(ok, true_match_score, best_distractor_score)``.
        """
        docs = [_VERIFY_TRUE_MATCH, *_VERIFY_DISTRACTORS]
        scores = self.score(_VERIFY_QUERY, docs)
        true_score = scores[0]
        best_distractor = max(scores[1:])
        return (true_score > best_distractor, true_score, best_distractor)
