#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向指定 HTTPS server 块幂等注入 /asf/ 反代 location。"""

import argparse
from dataclasses import dataclass, field
import os
import re
import shutil
import sys
import tempfile


DEFAULT_CONF = "/etc/nginx/sites-available/YOUR_SITE"
DEFAULT_PORT = 17001
MARKER = "asf-bridge-location"


@dataclass(frozen=True)
class Token:
    value: str
    start: int
    end: int
    kind: str = "word"


@dataclass
class Block:
    header: list[Token]
    start: int
    open_end: int
    items: list = field(default_factory=list)
    close_start: int = -1
    end: int = -1


def tokenize(text: str):
    """Tokenize nginx structure while ignoring braces in quotes/comments."""
    tokens = []
    comments = []
    i = 0
    punctuation = "{};"
    while i < len(text):
        char = text[i]
        if char.isspace():
            i += 1
            continue
        if char == "#":
            start = i
            end = text.find("\n", i)
            if end < 0:
                end = len(text)
            comments.append(Token(text[i + 1:end], start, end, "comment"))
            i = end
            continue
        if char in "'\"":
            quote = char
            start = i
            i += 1
            value = []
            while i < len(text):
                if text[i] == "\\" and i + 1 < len(text):
                    value.append(text[i + 1])
                    i += 2
                elif text[i] == quote:
                    i += 1
                    break
                else:
                    value.append(text[i])
                    i += 1
            tokens.append(Token("".join(value), start, i, "word"))
            continue
        if char in punctuation:
            tokens.append(Token(char, i, i + 1, char))
            i += 1
            continue
        start = i
        while i < len(text) and not text[i].isspace() and text[i] not in punctuation + "#\"'":
            i += 1
        if i == start:
            i += 1
            continue
        tokens.append(Token(text[start:i], start, i, "word"))
    return tokens, comments


def parse_items(tokens, start=0, nested=False):
    """Parse directives and blocks, retaining source offsets for safe edits."""
    items = []
    directive = []
    i = start
    while i < len(tokens):
        token = tokens[i]
        if token.kind == ";":
            if directive:
                items.append(directive)
                directive = []
            i += 1
        elif token.kind == "{":
            block = Block(
                header=directive,
                start=directive[0].start if directive else token.start,
                open_end=token.end,
            )
            directive = []
            block.items, i = parse_items(tokens, i + 1, nested=True)
            if i > 0 and i <= len(tokens) and tokens[i - 1].kind == "}":
                block.close_start = tokens[i - 1].start
                block.end = tokens[i - 1].end
            items.append(block)
        elif token.kind == "}":
            if directive:
                items.append(directive)
            if nested:
                return items, i + 1
            i += 1
        else:
            directive.append(token)
            i += 1
    if directive:
        items.append(directive)
    return items, i


def walk_blocks(items):
    for item in items:
        if isinstance(item, Block):
            yield item
            yield from walk_blocks(item.items)


def _walk_http_or_root_servers(items, inside_stream=False):
    for item in items:
        if not isinstance(item, Block):
            continue
        context = _value(item.header[0]) if item.header else ""
        if context == "server" and not inside_stream:
            yield item
        yield from _walk_http_or_root_servers(
            item.items, inside_stream or context == "stream"
        )


def direct_directives(block):
    return [item for item in block.items if isinstance(item, list) and item]


def _value(token):
    return token.value


def _is_location_asf(block):
    values = [_value(token) for token in block.header]
    return len(values) >= 2 and values[0] == "location" and values[1] == "/asf/"


def _marked_location_spans(text, blocks, comments):
    spans = []
    markers = [comment for comment in comments if comment.value.strip() == MARKER]
    for block in blocks:
        if not _is_location_asf(block) or block.end < 0:
            continue
        for marker in markers:
            if marker.end <= block.start and not text[marker.end:block.start].strip():
                start = text.rfind("\n", 0, marker.start) + 1
                if text[start:marker.start].strip():
                    start = marker.start
                end = block.end
                line_end = text.find("\n", end)
                if line_end >= 0 and not text[end:line_end].strip():
                    end = line_end + 1
                spans.append((start, end))
                break
    return spans


def _remove_spans(text, spans):
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + text[end:]
    return text


def _listen_port(directive):
    values = [_value(token) for token in directive]
    if not values or values[0] != "listen" or len(values) < 2:
        return None
    endpoint = values[1]
    if endpoint.isdigit():
        return int(endpoint)
    match = re.search(r":([0-9]+)$", endpoint)
    return int(match.group(1)) if match else None


def _server_names(block):
    names = []
    for directive in direct_directives(block):
        if _value(directive[0]) == "server_name":
            names.extend(_value(token) for token in directive[1:])
    return names


def _domain_matches(server_name, domain):
    name = server_name.lower().rstrip(".")
    host = domain.lower().rstrip(".")
    if name == host:
        return True
    if name.startswith("*."):
        suffix = name[1:]
        return host.endswith(suffix) and len(host) > len(suffix)
    if name.startswith("."):
        return host == name[1:] or host.endswith(name)
    return False


def find_https_servers(text):
    tokens, comments = tokenize(text)
    items, _ = parse_items(tokens)
    servers = []
    for block in _walk_http_or_root_servers(items):
        if any(_listen_port(directive) == 443 for directive in direct_directives(block)):
            servers.append(block)
    return servers, items, comments


def _choose_server(servers, domains):
    if domains:
        matching = [
            block
            for block in servers
            if any(
                _domain_matches(name, domain)
                for name in _server_names(block)
                for domain in domains
            )
        ]
    else:
        matching = servers
    if not matching:
        if domains:
            raise ValueError("cannot find a 443 server matching --domain: " + ", ".join(domains))
        raise ValueError("cannot find an nginx server listening on port 443")
    if len(matching) > 1:
        if domains:
            raise ValueError("--domain matches multiple 443 server blocks; refusing ambiguous edit")
        raise ValueError("multiple 443 server blocks found; specify --domain")
    return matching[0]


def _location_block(indent, port):
    inner = indent + "    "
    return "\n".join(
        [
            indent + "# " + MARKER,
            indent + "location /asf/ {",
            inner + f"proxy_pass http://127.0.0.1:{port};",
            inner + "proxy_http_version 1.1;",
            inner + "proxy_set_header Host $host;",
            inner + "proxy_set_header X-Real-IP $remote_addr;",
            inner + "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            inner + "proxy_set_header X-Forwarded-Proto $scheme;",
            indent + "}",
            "",
        ]
    )


def _insert_location(text, server, port):
    if server.close_start < 0:
        raise ValueError("malformed server block: missing closing brace")
    close_line_start = text.rfind("\n", 0, server.close_start) + 1
    close_indent = text[close_line_start:server.close_start]
    if close_indent.strip():
        close_line_start = server.close_start
        close_indent = ""

    server_line_start = text.rfind("\n", 0, server.start) + 1
    server_indent = text[server_line_start:server.start]
    directives = direct_directives(server)
    if directives:
        first_start = directives[0][0].start
        first_line_start = text.rfind("\n", 0, first_start) + 1
        body_indent = text[first_line_start:first_start]
        if body_indent.strip():
            body_indent = server_indent + "    "
    else:
        body_indent = server_indent + "    "

    insertion = _location_block(body_indent, port)
    return text[:close_line_start] + insertion + text[close_line_start:]


def inject(conf, domains=None, port=DEFAULT_PORT):
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("--port must be an integer from 1 to 65535")
    # sites-enabled commonly contains symlinks; update the real configuration,
    # rather than replacing the symlink itself during the atomic write.
    conf = os.path.realpath(conf)
    with open(conf, "r", encoding="utf-8") as stream:
        original = stream.read()

    tokens, comments = tokenize(original)
    items, _ = parse_items(tokens)
    all_blocks = list(walk_blocks(items))
    spans = _marked_location_spans(original, all_blocks, comments)
    cleaned = _remove_spans(original, spans)

    servers, items, _ = find_https_servers(cleaned)
    target = _choose_server(servers, domains or [])
    for item in target.items:
        if isinstance(item, Block):
            values = [_value(token) for token in item.header]
            if values in (["location", "/asf/"], ["location", "^~", "/asf/"],
                          ["location", "/asf"], ["location", "^~", "/asf"]):
                raise ValueError("target already has an unmarked /asf/ location; review it before injection")
    final = _insert_location(cleaned, target, port)
    if final == original:
        return False, len(spans), conf + ".bak-asf-bridge"

    backup = conf + ".bak-asf-bridge"
    if not os.path.exists(backup):
        shutil.copy2(conf, backup)
    directory = os.path.dirname(os.path.abspath(conf))
    fd, temp_path = tempfile.mkstemp(prefix=".asf-nginx-", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(final)
        shutil.copystat(conf, temp_path)
        os.replace(temp_path, conf)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return True, len(spans), backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("CONF", nargs="?", default=DEFAULT_CONF)
    parser.add_argument("--domain", action="append", default=[], help="HTTPS server_name to select; may be repeated")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    env_domains = [
        value.strip()
        for value in os.environ.get("ASF_NAV_DOMAIN", "").split(",")
        if value.strip()
    ]
    domains = args.domain or env_domains
    try:
        changed, removed, backup = inject(args.CONF, domains, args.port)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    suffix = f" (removed {removed} previously marked block(s))" if removed else ""
    status = "injected" if changed else "already current"
    print(f"nginx location {status}{suffix}; backup at {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
