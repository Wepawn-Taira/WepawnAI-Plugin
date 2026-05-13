"""
FOMOD ModuleConfig.xml 존재 여부 판별만 제공 (옵션 XML 파싱·LLM 분석 없음).

실제 아카이브 스캔은 :mod:`utils.fomod_parser`. :func:`extract_fomod_xml` 에 위임한다.
"""

from __future__ import annotations

from .fomod_parser import NO_FOMOD_MESSAGE

FOMOD_EXTRACT_ERROR_PREFIXES: tuple[str, ...] = (
    "파일을 찾을",
    "ZIP 파일이",
    "아카이브를 읽는",
    "지원하지 않는 아카이브",
)


def fomod_extract_indicates_moduleconfig_wizard(xml_or_msg: str) -> bool:
    """
    ``extract_fomod_xml`` 반환 문자열이 읽기 오류·FOMOD 부재가 아니면 True
    (즉 ModuleConfig 기반 설치 마법사가 있는 것으로 본다).
    """
    s = xml_or_msg or ""
    if any(s.startswith(p) for p in FOMOD_EXTRACT_ERROR_PREFIXES):
        return False
    return s != NO_FOMOD_MESSAGE
