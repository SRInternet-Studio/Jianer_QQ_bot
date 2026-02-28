import argparse
import asyncio
import os
import sys
from typing import Optional


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--query", type=str, default=None)
    parser.add_argument("--top-k", type=int, default=None)
    return parser.parse_args()


async def _build_index(force: bool) -> bool:
    import plugins.KnowledgeAnswer as knowledge

    ensure_async = getattr(knowledge, "ensure_index_ready_async", None)
    if ensure_async is not None:
        return bool(await ensure_async(force=force))
    ensure_sync = getattr(knowledge, "ensure_index_ready", None)
    if ensure_sync is not None:
        return bool(ensure_sync(force=force))
    return False


async def _retrieve(query: str, top_k: Optional[int]):
    import plugins.KnowledgeAnswer as knowledge

    retrieve_async = getattr(knowledge, "retrieve_knowledge_async", None)
    if retrieve_async is not None:
        return await retrieve_async(query, top_k)
    retrieve_sync = getattr(knowledge, "retrieve_knowledge", None)
    if retrieve_sync is not None:
        return retrieve_sync(query, top_k)
    return "", []


async def main():
    args = _parse_args()
    ok = await _build_index(force=bool(args.force))
    if not ok:
        print("index: failed")
        try:
            import plugins.KnowledgeAnswer as knowledge

            status_fn = getattr(knowledge, "get_index_status", None)
            if status_fn is not None:
                print("status:", status_fn())
        except Exception:
            pass
        return 1

    print("index: ok")
    if args.query:
        evidence, sources = await _retrieve(args.query, args.top_k)
        print("evidence:")
        print(evidence or "")
        print("sources:")
        print(sources)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
