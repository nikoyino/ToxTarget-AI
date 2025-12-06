# -*- coding: utf-8 -*-
"""
Literature Services Module - Extended Version

Contains base search services (PubMed, EuropePMC), a coordinator,
and the intelligent SmartLiteratureSearcher moved from core_engine.
"""

import aiohttp
import asyncio
import logging
import time
import random
import math
import json
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime
from xml.etree import ElementTree as ET
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

try:
    import fitz
except ImportError:
    fitz = None

logger = logging.getLogger(__name__)

# Import utilities for token estimation and text chunking
from utils import estimate_tokens, split_text_into_chunks

# Enforce English language for all AI interactions
LANGUAGE_CONSTRAINT = "\n\n**CRITICAL: All responses must be in English. Do not use Chinese or any other language.**\n"


def _get_random_user_agent() -> str:
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    ]
    return random.choice(user_agents)


class PubMedService:
    """PubMed E-utilities API service"""
    BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self, email: str, rate_limit: float = 0.34):
        self.email = email
        self.rate_limit = rate_limit
        self.last_request_time = 0

    async def _wait_rate_limit(self):
        elapsed = time.time() - self.last_request_time
        if elapsed < self.rate_limit:
            await asyncio.sleep(self.rate_limit - elapsed)
        self.last_request_time = time.time()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        reraise=True
    )
    async def search(
        self,
        query: str,
        limit: int = 20,
        start_year: str = '',
        end_year: str = '',
        sort: str = 'relevance'
    ) -> List[str]:
        """Search and return PMID list"""
        await self._wait_rate_limit()

        params = {
            'db': 'pubmed',
            'term': query,
            'retmax': limit,
            'retmode': 'json',
            'email': self.email,
            'sort': sort
        }

        if start_year and end_year:
            params['mindate'] = f"{start_year}/01/01"
            params['maxdate'] = f"{end_year}/12/31"
            params['datetype'] = 'pdat'

        url = f"{self.BASE_URL}/esearch.fcgi"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    response.raise_for_status()
                    data = await response.json()

                    id_list = data.get('esearchresult', {}).get('idlist', [])
                    count = data.get('esearchresult', {}).get('count', '0')

                    logger.info(f"PubMed search: found {count}, returning {len(id_list)}")
                    return id_list

        except Exception as e:
            logger.error(f"PubMed search failed: {e}")
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        reraise=True
    )
    async def fetch_details(self, pmids: List[str]) -> List[Dict]:
        """Fetch article details by PMID list"""
        if not pmids:
            return []

        await self._wait_rate_limit()

        params = {
            'db': 'pubmed',
            'id': ','.join(pmids),
            'retmode': 'xml',
            'email': self.email
        }

        url = f"{self.BASE_URL}/efetch.fcgi"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=60)) as response:
                    response.raise_for_status()
                    xml_text = await response.text()

                    return self._parse_pubmed_xml(xml_text)

        except Exception as e:
            logger.error(f"PubMed fetch details failed: {e}")
            raise

    def _parse_pubmed_xml(self, xml_text: str) -> List[Dict]:
        """Parse PubMed XML response"""
        try:
            root = ET.fromstring(xml_text)
            articles = []

            for article_xml in root.findall('.//PubmedArticle'):
                article_info = article_xml.find('.//Article')
                medline_info = article_xml.find('.//MedlineCitation')

                if article_info is None or medline_info is None:
                    continue

                pmid = medline_info.findtext('PMID', 'N/A').strip()
                title = article_info.findtext('ArticleTitle', 'No title').strip()

                authors = []
                for author in article_info.findall('.//Author'):
                    last_name = author.findtext('LastName', '').strip()
                    fore_name = author.findtext('ForeName', '').strip()
                    if last_name:
                        authors.append(f"{fore_name} {last_name}".strip())

                journal_node = article_info.find('.//Journal')
                journal_title = journal_node.findtext('Title', 'Unknown').strip() if journal_node is not None else 'Unknown'

                pub_date_node = article_info.find('.//PubDate')
                year = 'Unknown'
                if pub_date_node is not None:
                    year = pub_date_node.findtext('Year', 'Unknown').strip()

                abstract_texts = []
                for abstract_text in article_info.findall('.//Abstract/AbstractText'):
                    if abstract_text.text:
                        abstract_texts.append(abstract_text.text.strip())
                abstract = '\n'.join(abstract_texts) if abstract_texts else 'No abstract'

                pmcid = None
                doi = None
                for article_id in article_xml.findall('.//ArticleId'):
                    id_type = article_id.get('IdType')
                    if id_type == 'pmc':
                        pmcid = article_id.text.strip()
                    elif id_type == 'doi':
                        doi = article_id.text.strip()

                articles.append({
                    'pmid': pmid,
                    'title': title,
                    'authors': ', '.join(authors) if authors else 'No authors',
                    'journal': journal_title,
                    'year': year,
                    'abstract': abstract,
                    'doi': doi,
                    'pmcid': pmcid,
                    'source': 'PubMed'
                })

            logger.info(f"Successfully parsed {len(articles)} PubMed articles")
            return articles

        except Exception as e:
            logger.error(f"PubMed XML parsing failed: {e}")
            return []


class EuropePMCService:
    """Europe PMC REST API service"""
    BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest"

    def __init__(self, rate_limit: float = 0.35):
        self.rate_limit = rate_limit
        self.last_request_time = 0

    async def _wait_rate_limit(self):
        elapsed = time.time() - self.last_request_time
        if elapsed < self.rate_limit:
            await asyncio.sleep(self.rate_limit - elapsed)
        self.last_request_time = time.time()

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        reraise=True
    )
    async def search(
        self,
        query: str,
        limit: int = 20,
        start_year: str = '',
        end_year: str = '',
        sort: str = 'RELEVANCE',
        open_access_only: bool = False,
        has_fulltext: bool = False,
        min_citations: int = 0
    ) -> List[Dict]:
        """Search Europe PMC"""
        await self._wait_rate_limit()

        if min_citations is None or not isinstance(min_citations, int):
            min_citations = 0
            logger.warning("min_citations was None or invalid, defaulting to 0")

        query_parts = [query]

        if start_year and end_year:
            query_parts.append(f"PUB_YEAR:[{start_year} TO {end_year}]")

        if open_access_only:
            query_parts.append("OPEN_ACCESS:Y")

        if has_fulltext:
            query_parts.append("HAS_FT:Y")

        if min_citations > 0:
            query_parts.append(f"CITED_BY_COUNT:[{min_citations} TO *]")

        final_query = " AND ".join(query_parts)

        params = {
            'query': final_query,
            'pageSize': limit,
            'format': 'json'
        }

        url = f"{self.BASE_URL}/search"

        logger.info(f"Europe PMC query: {final_query}")

        try:
            async with aiohttp.ClientSession() as session:
                timeout_seconds = 120 if min_citations > 0 else 40

                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds)
                ) as response:

                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Europe PMC error {response.status}: {error_text[:200]}")

                        if response.status == 503:
                            raise Exception("Europe PMC service unavailable (503)")
                        elif response.status == 429:
                            raise Exception("Europe PMC rate limit exceeded (429)")
                        else:
                            response.raise_for_status()

                    data = await response.json()

                    hit_count = data.get('hitCount', 0)
                    logger.info(f"Europe PMC: {hit_count} total hits")

                    articles = self._parse_response(data)

                    logger.info(f"Successfully parsed {len(articles)} Europe PMC articles")
                    return articles

        except asyncio.TimeoutError as e:
            logger.error(f"Europe PMC search timeout after {timeout_seconds}s")

            if min_citations > 0:
                logger.info("Attempting fallback: local citation filtering")
                return await self._search_with_local_filter(
                    query, limit, start_year, end_year,
                    open_access_only, has_fulltext, min_citations
                )
            raise

        except Exception as e:
            logger.error(f"Europe PMC search failed: {e}")
            raise

    async def _search_with_local_filter(
        self,
        query: str,
        limit: int,
        start_year: str,
        end_year: str,
        open_access_only: bool,
        has_fulltext: bool,
        min_citations: int
    ) -> List[Dict]:
        """Fallback: fetch more results and filter locally by citations"""
        if min_citations is None or not isinstance(min_citations, int):
            min_citations = 0
            logger.warning("min_citations was None or invalid in local filter, defaulting to 0")

        try:
            logger.info(f"Fallback: fetching {limit * 3} results for local filtering")

            articles = await self.search(
                query=query,
                limit=limit * 3,
                start_year=start_year,
                end_year=end_year,
                open_access_only=open_access_only,
                has_fulltext=has_fulltext,
                min_citations=0
            )

            filtered = [
                article for article in articles
                if article.get('citationCount', 0) >= min_citations
            ]

            result = filtered[:limit]
            logger.info(f"Local filter: {len(filtered)} articles with >={min_citations} citations")

            return result

        except Exception as e:
            logger.error(f"Fallback strategy failed: {e}")
            return []

    def _parse_response(self, data: Dict) -> List[Dict]:
        """Parse Europe PMC JSON response with improved empty result handling"""
        articles = []

        if not data or not isinstance(data, dict):
            logger.warning("Europe PMC returned invalid data format")
            return []

        result_list = data.get('resultList', {})
        if not result_list or not isinstance(result_list, dict):
            logger.info("No resultList in Europe PMC response (query returned 0 results)")
            return []

        results = result_list.get('result', [])
        if not results or not isinstance(results, list):
            logger.info("Empty result array in Europe PMC response (0 matching articles)")
            return []

        for item in results:
            try:
                pmid = item.get('pmid', '').strip()
                pmcid = item.get('pmcid', '').strip()
                doi = item.get('doi', '').strip()

                if not (pmid or doi or pmcid):
                    continue

                citation_count = item.get('citedByCount', 0)
                if citation_count is None:
                    citation_count = 0

                article = {
                    'pmid': pmid,
                    'pmcid': pmcid,
                    'doi': doi,
                    'title': item.get('title', 'No title'),
                    'abstract': item.get('abstractText', 'No abstract'),
                    'authors': item.get('authorString', 'No authors'),
                    'journal': item.get('journalTitle', 'Unknown'),
                    'year': str(item.get('pubYear', 'Unknown')),
                    'citationCount': citation_count,
                    'isOpenAccess': item.get('isOpenAccess', 'N').upper() == 'Y',
                    'hasFullText': item.get('hasPDF', 'N').upper() == 'Y',
                    'source': 'EuropePMC'
                }

                articles.append(article)

            except Exception as e:
                logger.warning(f"Failed to parse Europe PMC result: {e}")
                continue

        return articles


class LiteratureSearchCoordinator:
    """Coordinate PubMed and Europe PMC searches with intelligent merging"""

    def __init__(self, pubmed_service: PubMedService, epmc_service: EuropePMCService):
        self.pubmed = pubmed_service
        self.epmc = epmc_service

    async def dual_search(
        self,
        query: str,
        limit: int = 20,
        start_year: str = '',
        end_year: str = '',
        sort: str = 'relevance',
        **epmc_filters
    ) -> Tuple[List[Dict], Dict]:
        """Parallel search on PubMed and Europe PMC with intelligent merging"""

        epmc_sort = 'RELEVANCE' if sort == 'relevance' else 'P_PDATE_D DESC'

        tasks = [
            self._search_pubmed(query, limit, start_year, end_year, sort),
            self._search_epmc(query, limit, start_year, end_year, epmc_sort, **epmc_filters)
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        pubmed_articles = results[0] if not isinstance(results[0], Exception) else []
        epmc_articles = results[1] if not isinstance(results[1], Exception) else []

        merged = self._merge_and_deduplicate(pubmed_articles, epmc_articles)

        stats = {
            'pubmed_count': len(pubmed_articles),
            'epmc_count': len(epmc_articles),
            'merged_count': len(merged),
            'duplicates_removed': len(pubmed_articles) + len(epmc_articles) - len(merged)
        }

        logger.info(
            f"Search complete: PubMed {stats['pubmed_count']}, "
            f"Europe PMC {stats['epmc_count']}, "
            f"merged {stats['merged_count']}"
        )

        return merged, stats

    async def _search_pubmed(
        self,
        query: str,
        limit: int,
        start_year: str,
        end_year: str,
        sort: str
    ) -> List[Dict]:
        """PubMed two-step search"""
        try:
            pmids = await self.pubmed.search(query, limit, start_year, end_year, sort)

            if not pmids:
                return []

            articles = await self.pubmed.fetch_details(pmids)

            return articles

        except Exception as e:
            logger.error(f"PubMed search workflow failed: {e}")
            return []

    async def _search_epmc(
        self,
        query: str,
        limit: int,
        start_year: str,
        end_year: str,
        sort: str,
        **filters
    ) -> List[Dict]:
        """Europe PMC search"""
        try:
            articles = await self.epmc.search(
                query=query,
                limit=limit,
                start_year=start_year,
                end_year=end_year,
                sort=sort,
                **filters
            )
            return articles

        except Exception as e:
            logger.error(f"Europe PMC search workflow failed: {e}")
            return []

    def _merge_and_deduplicate(
        self,
        pubmed_articles: List[Dict],
        epmc_articles: List[Dict]
    ) -> List[Dict]:
        """Merge and deduplicate (PubMed takes priority, enhances with EPMC data)"""
        merged = {}

        for article in pubmed_articles:
            key = self._get_article_key(article)
            if key:
                article['source'] = 'PubMed'
                merged[key] = article

        for article in epmc_articles:
            key = self._get_article_key(article)
            if key and key not in merged:
                article['source'] = 'EuropePMC'
                merged[key] = article
            elif key and key in merged:
                pubmed_entry = merged[key]
                if not pubmed_entry.get('citationCount') and article.get('citationCount'):
                    pubmed_entry['citationCount'] = article['citationCount']
                if not pubmed_entry.get('isOpenAccess') and article.get('isOpenAccess'):
                    pubmed_entry['isOpenAccess'] = article['isOpenAccess']
                if not pubmed_entry.get('hasFullText') and article.get('hasFullText'):
                    pubmed_entry['hasFullText'] = article['hasFullText']
                if not pubmed_entry.get('pmcid') and article.get('pmcid'):
                     pubmed_entry['pmcid'] = article['pmcid']
                if not pubmed_entry.get('doi') and article.get('doi'):
                    pubmed_entry['doi'] = article['doi']
                if 'sources' not in pubmed_entry:
                     pubmed_entry['sources'] = ['PubMed']
                if 'EuropePMC' not in pubmed_entry['sources']:
                     pubmed_entry['sources'].append('EuropePMC')

        return list(merged.values())

    def _get_article_key(self, article: Dict) -> Optional[str]:
        """Generate unique article identifier prioritizing PMID, then DOI"""
        pmid = article.get('pmid')
        doi = article.get('doi')

        if pmid and pmid != 'N/A':
            return f"pmid:{pmid}"
        elif doi:
            doi_normalized = doi.lower().strip()
            return f"doi:{doi_normalized}"
        else:
            title = article.get('title', '').strip().lower()
            year = article.get('year', '')
            if title and year and len(title) > 20:
                return f"title:{title[:50]}_{year}"
            return None


    async def get_pdf_full_text(self, article: Dict, email: str = None) -> Tuple[Optional[str], Optional[bytes]]:
        """Get PDF full text with 403 error handling"""
        if not fitz:
            logger.debug("PyMuPDF unavailable, skipping PDF")
            return None, None

        pmcid = article.get('pmcid')
        if not pmcid:
            logger.debug(f"No PMCID for {article.get('pmid', 'unknown')}")
            return None, None

        # --- FIX: PRIORITIZE EUROPE PMC TO AVOID NCBI 403 ---
        pdf_urls = [
            f'https://europepmc.org/articles/{pmcid}?pdf=render',
            f'https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/',
        ]

        await self.pubmed._wait_rate_limit()

        for url_idx, pdf_url in enumerate(pdf_urls):
            try:
                # Dynamic headers based on target
                headers = {
                    'User-Agent': _get_random_user_agent(),
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Connection': 'keep-alive',
                }
                
                if 'ncbi.nlm.nih.gov' in pdf_url:
                    headers['Referer'] = f'https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/'
                else:
                    headers['Referer'] = 'https://europepmc.org/'

                if url_idx > 0:
                    await asyncio.sleep(random.uniform(1.0, 2.0))

                logger.info(f"PDF download attempt {url_idx+1}/{len(pdf_urls)}: {pdf_url}")

                async with aiohttp.ClientSession() as session:
                    async with session.get(pdf_url, headers=headers, timeout=aiohttp.ClientTimeout(total=60), allow_redirects=True) as response:
                        if response.status == 403:
                            logger.warning(f"PDF download forbidden (403): {pdf_url}")
                            continue

                        content_type = response.headers.get('Content-Type', '')

                        if response.status == 200 and 'application/pdf' in content_type:
                            pdf_content = await response.read()

                            if len(pdf_content) < 1000:
                                logger.warning(f"Downloaded PDF is too small ({len(pdf_content)} bytes): {pdf_url}")
                                continue

                            try:
                                doc = fitz.open(stream=pdf_content, filetype="pdf")
                                full_text = "".join(page.get_text() for page in doc)
                                doc.close()

                                if len(full_text.strip()) < 100:
                                    logger.warning(f"Extracted text is too short ({len(full_text.strip())} chars): {pdf_url}")
                                    continue

                                logger.info(f"PDF downloaded and text extracted from {pdf_url}")
                                return full_text, pdf_content
                            except Exception as parse_e:
                                logger.error(f"Error parsing PDF from {pdf_url}: {parse_e}")
                                continue

                        else:
                            logger.warning(f"Unexpected status ({response.status}) or content type ({content_type}) for {pdf_url}")
                            continue

            except asyncio.TimeoutError:
                logger.warning(f"Timeout downloading PDF: {pdf_url}")
                continue
            except aiohttp.ClientError as client_e:
                logger.warning(f"Network error downloading PDF from {pdf_url}: {client_e}")
                continue
            except Exception as e:
                logger.warning(f"Unexpected error during PDF download from {pdf_url}: {str(e)}")
                continue

        logger.warning(f"All PDF download attempts failed for {pmcid}")
        return None, None


class SmartLiteratureSearcher:
    """AI-powered intelligent literature search"""

    def __init__(self, bio_service, ai_service, rate_limiter, engine=None):
        self.bio_service = bio_service
        self.ai_service = ai_service
        self.rate_limiter = rate_limiter
        self.search_cache = {}
        self.engine = engine
        self._alias_cache = {}

    def _log(self, message: str, level: str = 'info'):
        """Internal logging wrapper"""
        if self.engine and hasattr(self.engine, 'emit_log'):
            self.engine.emit_log(message, level)
        else:
            log_level = getattr(logging, level.upper(), logging.INFO)
            logger.log(log_level, message)

    async def _get_search_aliases(self, protein_symbol: str) -> List[str]:
        """
        Uses AI to find common aliases (like PXR) and orthologs (like NR1I2)
        for a given protein symbol to broaden literature search.
        """
        if protein_symbol in self._alias_cache:
            return self._alias_cache[protein_symbol]

        self._log(f"   Generating search aliases for {protein_symbol}...", 'info')
        
        system_prompt = f"""You are a bioinformatics expert. Your task is to provide critical search aliases for a protein.
{LANGUAGE_CONSTRAINT}
[Required JSON Output]
Return ONLY a JSON object with a single key "aliases", containing a list of strings.
{{
  "aliases": ["Alias1", "Alias2", ...]
}}"""
        
        user_prompt = f"""For the protein symbol "{protein_symbol}", list its most critical search aliases.
Include:
1.  The symbol itself.
2.  The most common protein name (e.g., PXR).
3.  The primary human ortholog symbol (e.g., NR1I2).
4.  The primary mouse ortholog symbol (e.g., Nr1i2).

Example: If I ask for "Nr1i2", you should return:
{{"aliases": ["Nr1i2", "PXR", "NR1I2"]}}

Example: If I ask for "ALB", you should return:
{{"aliases": ["ALB", "Albumin", "HSA"]}}

Example: If I ask for "CYP1A1", you should return:
{{"aliases": ["CYP1A1", "Cyp1a1"]}}

Now, provide the aliases for "{protein_symbol}"."""

        try:
            response = await self.ai_service.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                is_json=True,
                temperature=0.0
            )
            
            if response and isinstance(response.get('aliases'), list):
                aliases = response['aliases']
                aliases.append(protein_symbol)
                unique_aliases = sorted(list(set([a for a in aliases if a and len(a) > 2])), key=len, reverse=True)
                
                self._log(f"   Aliases for {protein_symbol}: {unique_aliases}", 'success')
                self._alias_cache[protein_symbol] = unique_aliases
                return unique_aliases
            else:
                raise ValueError("AI response did not contain a valid 'aliases' list.")

        except Exception as e:
            self._log(f"   AI alias generation failed for {protein_symbol}: {e}. Defaulting to symbol only.", 'warn')
            logger.error(f"AI alias generation failed for {protein_symbol}", exc_info=True)
            self._alias_cache[protein_symbol] = [protein_symbol]
            return [protein_symbol]

    async def _build_alias_query_string(self, protein_symbol: str) -> str:
        """Helper to get aliases and format them as an OR query string."""
        aliases = await self._get_search_aliases(protein_symbol)
        if not aliases:
            return f'"{protein_symbol}"'
        
        return "(" + " OR ".join([f'"{alias}"' for alias in aliases]) + ")"

    async def smart_search_pollutant_background(
        self,
        pollutant: str,
        target_tissue: str,
        target_phenotypes: List[str],
        email: str,
        limit: int = 15,
        start_year: str = '',
        end_year: str = '',
        epmc_filters: Dict = None
    ) -> Dict:
        """Search pollutant background literature"""
        self._log(f"{'='*60}")
        self._log(f"Pollutant Background Literature Search: {pollutant}")
        self._log(f"{'='*60}")

        search_strategies = []

        search_strategies.append({
            'name': f'{pollutant} Toxicology Reviews',
            'query': f'("{pollutant}") AND (toxicity OR "toxic mechanism" OR "adverse effect") AND (review[Publication Type] OR "systematic review"[Publication Type])',
            'priority': 'high',
            'type': 'toxicology_review',
            'limit': int(limit * 0.3)
        })

        search_strategies.append({
            'name': f'{pollutant} General Toxicology',
            'query': f'("{pollutant}") AND (toxicity OR "toxic mechanism" OR "adverse effect" OR hepatotoxicity OR nephrotoxicity)',
            'priority': 'medium',
            'type': 'toxicology_general',
            'limit': int(limit * 0.3)
        })

        if target_phenotypes:
            phenotype_terms = ' OR '.join([f'"{p}"' for p in target_phenotypes[:2]])
            search_strategies.append({
                'name': f'{pollutant} and Target Phenotypes',
                'query': f'("{pollutant}") AND ({phenotype_terms})',
                'priority': 'high',
                'type': 'epidemiology',
                'limit': int(limit * 0.2)
            })

        search_strategies.append({
            'name': f'{pollutant} in {target_tissue}',
            'query': f'("{pollutant}") AND ("{target_tissue}") AND ("molecular mechanism" OR signaling OR pathway OR "gene expression")',
            'priority': 'medium',
            'type': 'mechanism',
            'limit': int(limit * 0.2)
        })

        all_articles = await self._execute_search_strategies(
            search_strategies, start_year, end_year, epmc_filters or {}, email
        )

        scored_articles = await self._score_pollutant_background_articles(
            articles=all_articles,
            pollutant=pollutant,
            target_tissue=target_tissue,
            target_phenotypes=target_phenotypes,
            limit=limit
        )

        # Step 4.1: Extract info from top pollutant background papers
        self._log("Extracting key information from top background articles...")
        extracted_count = 0
        for article in scored_articles[:5]: # Extract from top 5
            if article.get('ai_relevance_score', 0) >= 5.0:
                try:
                    # Attempt to get full text for scored articles
                    if article.get('pmcid') and not article.get('full_text'):
                         self._log(f"   Attempting to fetch full-text for background article {article.get('pmid')}...")
                         full_text, pdf_binary = await self.bio_service.literature_coordinator.get_pdf_full_text(article, email)
                         if full_text:
                             article['full_text'] = full_text
                             article['full_text_source'] = "PDF Scrape"
                             logger.info(f"   Successfully retrieved full text via PDF for background article.")

                    extracted = await self.extract_literature_key_information(
                        article=article,
                        extraction_type='pollutant_background',
                        pollutant=pollutant
                    )
                    article['extracted_info'] = extracted
                    self._log(f"   ✓ Extracted from PMID {article.get('pmid')}")

                    if self.engine and hasattr(self.engine, 'notebook') and self.engine.notebook and extracted.get('extraction_type'):
                        self.engine.notebook.add_pollutant_background_note(extracted)
                        extracted_count += 1

                except Exception as e:
                    self._log(f"   ✗ Extraction failed for background PMID {article.get('pmid')}: {e}", 'warn')
        
        self._log(f"   Background extraction complete: {extracted_count} articles processed.", 'success')

        toxicology_articles = [a for a in scored_articles if a.get('literature_type') in ['toxicology_review', 'toxicology_general']]
        epidemiology_articles = [a for a in scored_articles if a.get('literature_type') == 'epidemiology']
        mechanism_articles = [a for a in scored_articles if a.get('literature_type') == 'mechanism']

        self._log(f"Pollutant Background Search Complete:")
        self._log(f"   {len(toxicology_articles)} toxicology reviews/studies", 'success')
        self._log(f"   {len(epidemiology_articles)} epidemiology studies", 'success')
        self._log(f"   {len(mechanism_articles)} mechanism papers", 'success')

        return {
            'articles': scored_articles,
            'toxicology_articles': toxicology_articles,
            'epidemiology_articles': epidemiology_articles,
            'mechanism_articles': mechanism_articles,
            'stage': 'pollutant_background',
            'quality_metrics': self._generate_quality_metrics(scored_articles)
        }

    async def smart_search_stage1(
        self,
        protein: str,
        pollutant: str,
        target_tissue: str,
        target_phenotypes: List[str],
        email: str,
        limit: int = 20,
        start_year: str = '',
        end_year: str = '',
        epmc_filters: Dict = None
    ) -> Dict:
        """Stage 1: Initial literature search with extraction"""
        self._log(f"{'='*60}")
        self._log(f"Stage 1 Literature Search: {protein} × {pollutant}")
        self._log(f"{'='*60}")

        function_quota = int(limit * 0.4)
        interaction_quota = int(limit * 0.6)

        epmc_filters = epmc_filters or {}

        self._log(f"Sub-task 1: Protein Function Literature...")
        function_articles = await self._search_protein_function(
            protein=protein,
            target_tissue=target_tissue,
            limit=function_quota,
            start_year=start_year,
            end_year=end_year,
            email=email,
            epmc_filters=epmc_filters
        )

        self._log(f"Sub-task 2: Protein-Pollutant Interaction Literature...")
        interaction_articles = await self._search_protein_pollutant_interaction(
            protein=protein,
            pollutant=pollutant,
            target_tissue=target_tissue,
            target_phenotypes=target_phenotypes,
            limit=interaction_quota,
            start_year=start_year,
            end_year=end_year,
            email=email,
            epmc_filters=epmc_filters
        )

        all_articles = self._deduplicate_and_finalize(function_articles + interaction_articles, limit * 2)

        scored_articles = await self._score_and_filter_articles(
            articles=all_articles,
            protein=protein,
            pollutant=pollutant,
            target_tissue=target_tissue,
            limit=limit
        )

        avg_score = sum(a.get('final_score', 0) for a in scored_articles) / len(scored_articles) if scored_articles else 0
        if avg_score < 5.0 and len(scored_articles) < limit:
            self._log(f"   Low average score ({avg_score:.1f}). Broadening function search...", 'warn')
            function_quota += 10
            extra_articles = await self._search_protein_function(protein, target_tissue, function_quota, start_year, end_year, broader=True, email=email, epmc_filters=epmc_filters)
            if extra_articles:
                scored_extra = await self._score_and_filter_articles(extra_articles, protein, pollutant, target_tissue, 10)
                scored_articles.extend(scored_extra)
                scored_articles = self._deduplicate_and_finalize(scored_articles, limit)

        top_articles = [a for a in scored_articles if a.get('ai_relevance_score', 0) >= 8]
        if top_articles:
            self._log(f"   Expanding citation network for {len(top_articles)} top articles...")
            top_pmids = [a['pmid'] for a in top_articles if a.get('pmid')]
            if top_pmids:
                citing_pmids = await self.bio_service.get_citation_network(top_pmids, email)
                if citing_pmids:
                    new_articles = await self.bio_service.literature_coordinator.pubmed.fetch_details(citing_pmids[:10])
                    if new_articles:
                        scored_new = await self._score_and_filter_articles(new_articles, protein, pollutant, target_tissue, 10)
                        scored_articles.extend(scored_new)
                        scored_articles = self._deduplicate_and_finalize(scored_articles, int(limit * 1.5))

        for article in scored_articles[:5]:
             if article.get('pmcid'):
                 self._log(f"   Attempting to fetch full-text for article {article.get('pmid') or article.get('doi')}...")
                 full_text, pdf_binary = await self.bio_service.literature_coordinator.get_pdf_full_text(article, email)
                 if full_text:
                     article['full_text'] = full_text
                     article['full_text_source'] = "PDF Scrape"
                     logger.info(f"   Successfully retrieved full text via PDF.")
                 if pdf_binary:
                     article['pdf_binary'] = pdf_binary

        self._log("Extracting key information from top articles...")
        extracted_count = 0
        # 1. Expand range to [:10] and 2. Lower threshold to 5.0
        for article in scored_articles[:10]:
            if article.get('ai_relevance_score', 0) >= 5.0:
                try:
                    lit_type = article.get('literature_type', 'interaction')
                    extraction_type = 'protein_function' if lit_type == 'function' else 'protein_interaction'

                    extracted = await self.extract_literature_key_information(
                        article, extraction_type, protein=protein, pollutant=pollutant
                    )
                    article['extracted_info'] = extracted
                    self._log(f"   ✓ Extracted from PMID {article.get('pmid')}")

                    if self.engine and hasattr(self.engine, 'notebook') and self.engine.notebook and extracted.get('extraction_type'):
                        self.engine.notebook.add_protein_analysis_note(protein, extracted)
                        extracted_count += 1

                except Exception as e:
                    self._log(f"   ✗ Extraction failed for PMID {article.get('pmid')}: {e}", 'warn')

        if extracted_count == 0 and scored_articles:
            self._log(f"   No articles met score >= 5.0 for {protein}. Failsafe: extracting top 1 article.", 'warn')
            top_article = scored_articles[0]
            try:
                lit_type = top_article.get('literature_type', 'interaction')
                extraction_type = 'protein_function' if lit_type == 'function' else 'protein_interaction'

                extracted = await self.extract_literature_key_information(
                    top_article, extraction_type, protein=protein, pollutant=pollutant
                )
                top_article['extracted_info'] = extracted
                self._log(f"   ✓ Failsafe extracted from PMID {top_article.get('pmid')}")

                if self.engine and hasattr(self.engine, 'notebook') and self.engine.notebook and extracted.get('extraction_type'):
                    self.engine.notebook.add_protein_analysis_note(protein, extracted)

            except Exception as e:
                self._log(f"   ✗ Failsafe extraction failed for PMID {top_article.get('pmid')}: {e}", 'error')

        function_lit = [a for a in scored_articles if a.get('literature_type') == 'function']
        interaction_lit = [a for a in scored_articles if a.get('literature_type') == 'interaction']

        self._log(f"Stage 1 Complete: {len(function_lit)} function articles, {len(interaction_lit)} interaction articles", 'success')

        return {
            'articles': scored_articles,
            'function_articles': function_lit,
            'interaction_articles': interaction_lit,
            'stage': 'stage1',
            'quality_metrics': self._generate_quality_metrics(scored_articles)
        }

    async def smart_search_stage2(
        self,
        protein: str,
        pollutant: str,
        target_tissue: str,
        knowledge_gaps: List[str],
        critique_points: List[str],
        email: str,
        limit: int = 10,
        start_year: str = '',
        end_year: str = '',
        epmc_filters: Dict = None
    ) -> Dict:
        """Stage 2: Targeted literature search"""
        self._log(f"{'='*60}")
        self._log(f"Stage 2 Targeted Literature Search: {protein}")
        self._log(f"{'='*60}")
        self._log("Knowledge Gaps to address:")
        for gap in knowledge_gaps[:3]:
            self._log(f"  - {gap}")
        self._log("Critique Points to address:")
        for point in critique_points[:3]:
            self._log(f"  - {point}")

        search_queries = await self._generate_targeted_queries(
            protein=protein,
            pollutant=pollutant,
            target_tissue=target_tissue,
            gaps=knowledge_gaps,
            critiques=critique_points
        )

        if not search_queries:
             self._log("No targeted queries generated, skipping Stage 2 search.", 'warn')
             return {'articles': [], 'stage': 'stage2', 'addresses_gaps': knowledge_gaps, 'addresses_critiques': critique_points, 'quality_metrics': {}}

        all_articles = await self._execute_search_strategies(
            search_queries, start_year, end_year, epmc_filters or {}, email
        )

        scored_articles = await self._score_targeted_articles(
            articles=all_articles,
            protein=protein,
            pollutant=pollutant,
            gaps=knowledge_gaps,
            critiques=critique_points
        )
        
        # Step 4.3: Extract info from Stage 2 papers
        self._log(f"Extracting key information from Stage 2 articles for {protein}...")
        extracted_count = 0
        for article in scored_articles: # Extract from all found Stage 2 articles
            try:
                # Attempt to get full text
                if article.get('pmcid') and not article.get('full_text'):
                     self._log(f"   Attempting to fetch full-text for Stage 2 article {article.get('pmid')}...")
                     full_text, pdf_binary = await self.bio_service.literature_coordinator.get_pdf_full_text(article, email)
                     if full_text:
                         article['full_text'] = full_text
                         article['full_text_source'] = "PDF Scrape"
                         logger.info(f"   Successfully retrieved full text via PDF for Stage 2 article.")

                # Use 'protein_interaction' as type, as Stage 2 is about mechanism
                extracted = await self.extract_literature_key_information(
                    article=article,
                    extraction_type='protein_interaction', 
                    protein=protein,
                    pollutant=pollutant
                )
                article['extracted_info'] = extracted
                self._log(f"   ✓ Extracted from Stage 2 PMID {article.get('pmid')}")

                if self.engine and hasattr(self.engine, 'notebook') and self.engine.notebook and extracted.get('extraction_type'):
                    self.engine.notebook.add_protein_analysis_note(protein, extracted)
                    extracted_count += 1

            except Exception as e:
                self._log(f"   ✗ Extraction failed for Stage 2 PMID {article.get('pmid')}: {e}", 'warn')
        
        self._log(f"   Stage 2 extraction complete: {extracted_count} articles processed.", 'success')

        final_articles = self._deduplicate_and_finalize(scored_articles, limit)

        self._log(f"Stage 2 Complete: {len(final_articles)} targeted articles found", 'success')

        return {
            'articles': final_articles,
            'stage': 'stage2',
            'addresses_gaps': knowledge_gaps,
            'addresses_critiques': critique_points,
            'quality_metrics': self._generate_quality_metrics(final_articles)
        }

    # Integrate _merge_extraction_results helper
    def _merge_extraction_results(self, results: List[Dict], extraction_type: str) -> Dict:
        """Merge results from multiple chunks"""
        if not results:
            return {}
        
        if len(results) == 1:
            return results[0]
        
        merged = {
            'pmid': results[0].get('pmid'),
            'title': results[0].get('title'),
            'extraction_type': extraction_type,
            'chunk_count': len(results)
        }
        
        # Keys that are lists and should be concatenated and de-duplicated
        list_keys = [
            'functional_link_to_phenotype', 'pollutant_impact_on_function',
            'key_phenotype_results_data', 'author_conclusions_on_phenotype',
            'toxic_effects', 'molecular_mechanisms', 'key_pathways', 'key_findings',
            'target_organs', 'primary_functions', 'pathways', 'molecular_interactions',
            'regulation_mechanisms', 'key_functional_findings', 'downstream_consequences',
            'experimental_methods', 'key_interaction_findings'
        ]
        
        for key in list_keys:
            all_items = []
            for r in results:
                if key in r and isinstance(r[key], list):
                    all_items.extend(r[key])
            if all_items:
                merged[key] = list(dict.fromkeys(all_items))

        # Keys that are strings and should be prioritized or combined
        string_keys = [
            'interaction_evidence', 'interaction_type', 'binding_details',
            'functional_impact', 'evidence_strength', 'dose_response_info'
        ]
        
        for key in string_keys:
            all_items = [r.get(key) for r in results if r.get(key) and r.get(key) not in ["Not specified", "N/A", "Unclear", ""]]
            if all_items:
                # Prioritize 'Yes' for evidence
                if key == 'interaction_evidence' and 'Yes' in all_items:
                    merged[key] = 'Yes'
                # Prioritize 'strong' for strength
                elif key == 'evidence_strength' and 'strong' in all_items:
                     merged[key] = 'strong'
                elif key == 'evidence_strength' and 'moderate' in all_items:
                     merged[key] = 'moderate'
                else:
                    merged[key] = all_items[0]
            else:
                 merged[key] = "Not specified"
        
        return merged

    async def _run_extraction_on_text(
        self,
        text_input: str,
        extraction_type: str,
        protein: str = None,
        pollutant: str = None
    ) -> Dict:
        """Helper to run the AI call for a single text blob (or chunk)."""
        system_prompt_content = f"""You are an expert biomedical data extractor. Your task is to analyze the provided text and extract specific information precisely according to the user's requested JSON format.
{LANGUAGE_CONSTRAINT}
Ensure all text in the JSON is in English."""

        user_prompt_content = ""
        if extraction_type == 'pollutant_background':
            user_prompt_content = self._create_pollutant_extraction_prompt(text_input, pollutant)
        elif extraction_type == 'protein_function':
            user_prompt_content = self._create_protein_function_extraction_prompt(text_input, protein)
        elif extraction_type == 'protein_interaction':
            user_prompt_content = self._create_interaction_extraction_prompt(text_input, protein, pollutant)
        else:
            self._log(f"Unknown extraction type: {extraction_type}", 'error')
            raise ValueError(f"Unknown extraction type: {extraction_type}")
        
        extracted = await self.ai_service.generate(
            system_prompt=system_prompt_content,
            user_prompt=user_prompt_content,
            is_json=True
        )
        return extracted if isinstance(extracted, dict) else {}


    async def extract_literature_key_information(
        self,
        article: Dict,
        extraction_type: str,
        protein: str = None,
        pollutant: str = None
    ) -> Dict:
        """
        Extract key information from literature using AI.
        V3 REPAIR: Handles full text chunking.
        """
        try:
            text_to_process = ""
            base_info = f"Title: {article.get('title', 'N/A')}\n\nAbstract: {article.get('abstract', 'N/A')}\n\n"

            if article.get('full_text'):
                text_to_process = article['full_text']
            else:
                text_to_process = base_info # Fallback to abstract only
            
            safe_limit = (self.ai_service.get_safe_token_limit() * 0.8) # 80% safety margin
            text_tokens = estimate_tokens(text_to_process)

            extracted_results = []

            if text_tokens <= safe_limit:
                # Text is short enough, process as one
                self._log(f"   Extracting from single text block (Tokens: {text_tokens}) for PMID {article.get('pmid')}", 'debug')
                # Use base_info if we only have abstract, otherwise use full text
                text_input = text_to_process if article.get('full_text') else base_info
                
                result = await self._run_extraction_on_text(
                    text_input, extraction_type, protein, pollutant
                )
                extracted_results.append(result)
            
            else:
                # Text is too long, chunk it
                self._log(f"   Text too long (Tokens: {text_tokens}). Splitting into chunks for PMID {article.get('pmid')}...", 'info')
                chunks = split_text_into_chunks(
                    text_to_process, 
                    max_tokens_per_chunk=int(safe_limit * 0.9), # 90% of safe limit for chunk
                    overlap_tokens=200
                )
                self._log(f"   Split into {len(chunks)} chunks.", 'info')

                for i, chunk in enumerate(chunks):
                    self._log(f"   Processing chunk {i+1}/{len(chunks)}...", 'debug')
                    # Prepend base info (Title/Abstract) to every chunk for context
                    chunk_with_context = f"{base_info}\n[TEXT CHUNK {i+1}/{len(chunks)}]\n{chunk}"
                    
                    result = await self._run_extraction_on_text(
                        chunk_with_context, extraction_type, protein, pollutant
                    )
                    extracted_results.append(result)

            # Merge results from all chunks (or the single result)
            merged = self._merge_extraction_results(extracted_results, extraction_type)
            
            # Add article metadata
            merged['pmid'] = article.get('pmid')
            merged['title'] = article.get('title')
            merged['year'] = article.get('year')
            merged['extraction_type'] = extraction_type
            return merged

        except Exception as e:
            self._log(f"   Failed to extract from PMID {article.get('pmid')}: {e}", 'warn')
            logger.error(f"Extraction failed for PMID {article.get('pmid')}", exc_info=True)
            return {
                'pmid': article.get('pmid'),
                'extraction_type': extraction_type,
                'error': str(e)
            }

    async def deep_extract_critical_papers(
        self,
        all_literature: Dict[str, Dict],
        top_proteins: List[str],
        max_papers: int = 10
    ) -> Dict[str, Dict]:
        """Extract detailed insights from high-quality papers for top proteins."""
        self._log("Deep extraction from critical papers", 'info')
        critical_extracts = {}
        extraction_count = 0

        for protein in top_proteins:
            if protein not in all_literature:
                self._log(f"  No literature data found for {protein}, skipping deep extraction.", 'warn')
                continue

            articles = all_literature[protein].get('articles', [])
            candidates = sorted(
                [a for a in articles if a.get('ai_relevance_score', 0) >= 8],
                key=lambda x: (1 if x.get('full_text') else 0, x.get('ai_relevance_score', 0)),
                reverse=True
            )[:3]

            for article in candidates:
                if extraction_count >= max_papers:
                    break

                pmid = article.get('pmid')
                if not pmid or str(pmid) in critical_extracts:
                    continue

                extract = await self._deep_extract_single_paper(article, protein)
                if extract:
                    critical_extracts[str(pmid)] = extract
                    extraction_count += 1
                    self._log(f"  ✓ Deep extracted PMID {pmid} for {protein}", 'success')

        self._log(f"Deep extraction complete: {extraction_count} papers processed.", 'success')
        return critical_extracts

    # Modified _deep_extract_single_paper to use chunking and new prompts
    async def _deep_extract_single_paper(
        self,
        article: Dict,
        protein: str
    ) -> Optional[Dict]:
        """
        Extract detailed quantitative data and mechanistic insights from a single paper.
        V3 REPAIR: Handles full text chunking and uses V3 prompts.
        """
        pmid = article.get('pmid', 'N/A')
        self._log(f"   Performing deep extraction for PMID {pmid} ({protein})...", 'info')

        text_to_process = ""
        base_info = f"Title: {article.get('title', 'N/A')}\n\nAbstract: {article.get('abstract', 'N/A')}\n\n"

        if article.get('full_text'):
            text_to_process = article['full_text']
        else:
            self._log(f"   Note: Full text not available for deep extract PMID {pmid}, using abstract only.", 'info')
            text_to_process = base_info # Fallback to abstract only

        safe_limit = (self.ai_service.get_safe_token_limit() * 0.8) # 80% safety margin
        text_tokens = estimate_tokens(text_to_process)
        
        extraction_type = "deep_extract" # Internal type for merging
        extracted_results = []
        
        try:
            if text_tokens <= safe_limit:
                # Text is short enough, process as one
                self._log(f"   Deep extracting from single text block (Tokens: {text_tokens}) for PMID {pmid}", 'debug')
                text_input = text_to_process if article.get('full_text') else base_info
                
                result = await self._run_deep_extraction_on_text(
                    text_input, protein, pmid, article.get('title', '')
                )
                extracted_results.append(result)
            
            else:
                # Text is too long, chunk it
                self._log(f"   Text too long (Tokens: {text_tokens}). Splitting for deep extract PMID {pmid}...", 'info')
                chunks = split_text_into_chunks(
                    text_to_process, 
                    max_tokens_per_chunk=int(safe_limit * 0.9),
                    overlap_tokens=200
                )
                self._log(f"   Split into {len(chunks)} chunks.", 'info')

                for i, chunk in enumerate(chunks):
                    self._log(f"   Processing deep chunk {i+1}/{len(chunks)}...", 'debug')
                    chunk_with_context = f"{base_info}\n[TEXT CHUNK {i+1}/{len(chunks)}]\n{chunk}"
                    
                    result = await self._run_deep_extraction_on_text(
                        chunk_with_context, protein, pmid, article.get('title', '')
                    )
                    extracted_results.append(result)

            merged = self._merge_extraction_results(extracted_results, extraction_type)
            
            if not merged or (not merged.get('functional_link_to_phenotype') and not merged.get('key_phenotype_results_data')):
                 self._log(f"   ✗ Deep extraction for PMID {pmid} yielded no key data after merge.", 'warn')
                 return None

            merged['protein_focus'] = protein
            merged['pmid'] = pmid # Ensure pmid is set
            self._log(f"   ✓ Deep extraction successful for PMID {pmid}", 'success')
            return merged

        except Exception as e:
            self._log(f"   ✗ Deep extraction failed for PMID {pmid}: {e}", 'error')
            logger.error(f"Deep extraction failed for PMID {pmid}", exc_info=True)
            return None

    async def _run_deep_extraction_on_text(
        self,
        content: str,
        protein: str,
        pmid: str,
        title: str
    ) -> Dict:
        """Helper to run the AI call for a single deep extraction chunk."""
        
        system_prompt = f"""Perform a deep analysis and extract specific findings from the provided scientific paper content, focusing on {protein}.
{LANGUAGE_CONSTRAINT}
[Extraction Tasks]
1.  **Functional-Phenotype Link:** What evidence connects {protein}'s function to the toxic phenotype (e.g., hepatotoxicity, steatosis)?
2.  **Pollutant Impact (Bonus):** Does this text mention the pollutant's effect on {protein}?
3.  **Key Phenotype Results:** Extract quantitative/qualitative data from 'Results' linking function to phenotype.
4.  **Author Conclusions:** Summarize mechanism hypotheses from 'Discussion' about the phenotype link.
5.  **Evidence Strength:** Assess the strength of the protein-phenotype link.

[Required JSON Output Format]
Return ONLY a JSON object. Use empty arrays `[]` or "Not specified" if information is absent.
{{
  "pmid": "{pmid}",
  "title": "{title[:100]}...",
  "functional_link_to_phenotype": ["【MUST ANSWER】Evidence linking protein function to the phenotype..."],
  "pollutant_impact_on_function": ["【BONUS/OPTIONAL】Impact of pollutant on function, or 'Not mentioned'"],
  "key_phenotype_results_data": ["Key quantitative/qualitative data from 'Results' section..."],
  "author_conclusions_on_phenotype": ["Mechanism hypothesis from 'Discussion' section..."],
  "evidence_strength": "strong / moderate / weak / speculative"
}}"""

        user_prompt = f"""
[Paper Content]
{content}
"""
        
        result = await self.ai_service.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            is_json=True
        )
        return result if isinstance(result, dict) else {}

    async def identify_knowledge_gaps(
        self,
        top_proteins: List[Dict],
        existing_literature: Dict[str, Dict],
        pollutant: str,
        target_phenotypes: List[str]
    ) -> List[Dict]:
        """Identify critical knowledge gaps requiring additional literature using AI."""
        self._log("Identifying knowledge gaps", 'info')

        evidence_summary = []
        for p_info in top_proteins[:5]:
            protein = p_info.get('protein')
            score = p_info.get('final_consensus_score', p_info.get('score', 0))

            lit_count = 0
            function_articles_count = 0
            interaction_articles_count = 0
            if protein and protein in existing_literature:
                protein_lit = existing_literature[protein]
                lit_count = len(protein_lit.get('articles', []))
                function_articles_count = len(protein_lit.get('function_articles', []))
                interaction_articles_count = len(protein_lit.get('interaction_articles', []))

            summary_line = (
                f"- {protein} (Score: {score:.1f}/10): "
                f"{lit_count} total articles ({function_articles_count} function, {interaction_articles_count} interaction)"
            )
            evidence_summary.append(summary_line)

        
        system_prompt = f"""As a toxicology research expert, you will critically analyze evidence gaps.
{LANGUAGE_CONSTRAINT}
[Task]
Identify 3-5 CRITICAL gaps where additional literature would significantly strengthen the analysis. Focus on gaps that prevent establishing a clear mechanistic link between pollutant binding, protein function alteration, and the target phenotypes.

[Required JSON Output]
Return a JSON object with a 'gaps' array. Each gap object MUST have 'gap_type', 'description', 'search_query', and 'priority' fields.
{{
  "gaps": [
    {{
      "gap_type": "mechanistic_link / dose_response / temporal_aspects / species_comparison / validation_evidence",
      "description": "Clearly state what specific information is missing (e.g., 'Lack of evidence linking {top_proteins[0]['protein']} inhibition to downstream pathway X activation').",
      "search_query": "Provide a specific, targeted PubMed query string designed to find literature addressing ONLY this gap (e.g., '({top_proteins[0]['protein']}) AND (inhibition OR downregulation) AND (pathway X) AND ({pollutant})').",
      "priority": "high / medium"
    }}
  ]
}}

[Instructions]
- Prioritize gaps related to the top-ranked proteins.
- Ensure 'search_query' is well-formed for PubMed.
- Focus on gaps likely fillable with existing literature (not proposing new experiments).
- Be specific in the 'description' and 'search_query'.
- Ensure all text is in English."""

        user_prompt = f"""
[Research Context]
- Pollutant: {pollutant}
- Target Phenotypes: {', '.join(target_phenotypes)}
- Top Proteins (ranked by likely importance): {', '.join([p.get('protein', '') for p in top_proteins[:5]])}

[Current Evidence Summary]
{chr(10).join(evidence_summary)}
Note: Articles classified as 'function' focus on the protein's role, while 'interaction' links the protein to the pollutant or phenotype.
"""

        try:
            response = await self.ai_service.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                is_json=True
            )
            
            
            gaps = response.get('gaps', [])

            validated_gaps = []
            if isinstance(gaps, list):
                for gap in gaps:
                    if isinstance(gap, dict) and all(k in gap for k in ['gap_type', 'description', 'search_query', 'priority']):
                        validated_gaps.append(gap)
                    else:
                         self._log(f"  Invalid gap structure found: {gap}", 'warn')
            else:
                self._log("  AI did not return a list of gaps.", 'warn')


            self._log(f"  Identified {len(validated_gaps)} potential knowledge gaps.", 'success')
            for gap in validated_gaps[:3]:
                self._log(f"    • [{gap.get('priority')}] {gap.get('gap_type')}: {gap.get('description', '')[:70]}...", 'info')

            return validated_gaps

        except Exception as e:
            self._log(f"Gap identification failed: {e}", 'error')
            logger.error(f"Gap identification failed: {e}", exc_info=True)
            return []

    async def targeted_gap_filling_search(
        self,
        gaps: List[Dict],
        email: str,
        limit_per_gap: int = 5,
        start_year: str = '',
        end_year: str = ''
    ) -> Dict[str, Dict]:
        """Conduct targeted searches to fill identified knowledge gaps."""
        self._log("Targeted gap-filling search initiated", 'info')
        additional_literature = {}

        high_priority_gaps = [g for g in gaps if g.get('priority') == 'high']
        gaps_to_search = high_priority_gaps[:3]

        if not gaps_to_search:
            self._log("  No high-priority gaps identified for targeted search.", 'info')
            return {}

        self._log(f"  Targeting {len(gaps_to_search)} high-priority gaps.", 'info')

        for gap in gaps_to_search:
            query = gap.get('search_query', '')
            gap_desc = gap.get('description', 'Unknown Gap')
            gap_key = gap.get('gap_type', hash(gap_desc))

            if not query:
                self._log(f"  Skipping gap '{gap_desc[:50]}...' - no search query provided.", 'warn')
                continue

            self._log(f"  Searching for gap: {gap_desc[:70]}...", 'info')
            self._log(f"    Query: {query}", 'info')

            try:
                articles, stats = await self.bio_service.literature_coordinator.dual_search(
                    query=query,
                    limit=limit_per_gap,
                    start_year=start_year or '2015',
                    end_year=end_year or str(datetime.now().year),
                )

                if articles:
                    for article in articles:
                        article['gap_addressed'] = gap_desc
                        article['ai_relevance_score'] = 5 + self._normalize_citation_score(article.get('citationCount', 0)) / 2
                        article['search_strategy'] = f"Gap Filling: {gap_key}"

                    additional_literature[gap_key] = {
                        'gap_description': gap_desc,
                        'query_used': query,
                        'articles': articles
                    }
                    self._log(f"    ✓ Found {len(articles)} articles relevant to the gap.", 'success')
                else:
                    self._log(f"    - No articles found for this specific gap query.", 'info')

                await asyncio.sleep(1.0)

            except Exception as e:
                self._log(f"    ✗ Gap-filling search failed for '{gap_desc[:50]}...': {e}", 'error')
                logger.warning(f"Gap-filling search failed for query '{query}': {e}")

        self._log(f"Targeted gap-filling search complete. Found literature for {len(additional_literature)} gaps.", 'success')
        return additional_literature

    async def _execute_search_strategies(
        self,
        strategies: List[Dict],
        start_year: str,
        end_year: str,
        epmc_filters: Dict,
        email: str
    ) -> List[Dict]:
        """Execute a list of search strategies using the dual literature search."""
        all_articles = []
        unique_keys = set()

        for strategy in strategies:
            if not isinstance(strategy, dict) or not strategy.get('query'):
                 self._log(f"   Skipping invalid strategy: {strategy}", 'warn')
                 continue

            try:
                self._log(f"   Executing strategy: {strategy.get('name', 'Unnamed Strategy')}...")
                articles, stats = await self.bio_service.literature_coordinator.dual_search(
                    query=strategy['query'],
                    limit=strategy.get('limit', 10),
                    start_year=start_year,
                    end_year=end_year,
                    sort='relevance',
                    **epmc_filters
                )

                new_articles_count = 0
                for article in articles:
                     article['literature_type'] = strategy.get('type', 'unknown')
                     article['search_strategy'] = strategy.get('name', 'unknown')

                     key = self.bio_service.literature_coordinator._get_article_key(article)
                     if key and key not in unique_keys:
                         all_articles.append(article)
                         unique_keys.add(key)
                         new_articles_count += 1

                self._log(f"   Found {stats['merged_count']} articles ({new_articles_count} new) for '{strategy.get('name', 'N/A')}'.")

            except Exception as e:
                self._log(f"   Strategy '{strategy.get('name', 'N/A')}' failed: {e}", 'error')
                logger.error(f"Search strategy '{strategy.get('name', 'N/A')}' failed", exc_info=True)

        self._log(f"   Total unique articles after all strategies: {len(all_articles)}")
        return all_articles


    async def _score_pollutant_background_articles(
        self,
        articles: List[Dict],
        pollutant: str,
        target_tissue: str,
        target_phenotypes: List[str],
        limit: int
    ) -> List[Dict]:
        """Score pollutant background literature using AI"""
        self._log("Scoring pollutant background articles...")

        if not articles:
            return []

        batch_size = 10
        scored_articles = []

        for i in range(0, len(articles), batch_size):
            batch = articles[i:i+batch_size]

            articles_summary = []
            for idx, article in enumerate(batch):
                articles_summary.append(
                    f"[{idx+1}] PMID:{article.get('pmid', 'N/A')} ({article.get('year', 'N/A')})\n"
                    f"Title: {article.get('title', 'N/A')}\n"
                    f"Abstract: {article.get('abstract', 'N/A')[:300]}...\n"
                )
            
            
            system_prompt = f"""As a toxicology expert, you will rate the relevance of these articles providing background information on {pollutant} toxicity, especially concerning {target_tissue} and phenotypes like {', '.join(target_phenotypes)}.
{LANGUAGE_CONSTRAINT}
[Research Context]
- Pollutant: {pollutant}
- Target Tissue: {target_tissue}
- Target Phenotypes: {', '.join(target_phenotypes)}
- Goal: Understand the general toxicity profile, mechanisms, and effects of {pollutant}.

[Scoring Criteria] (Rate each article from 1 to 10)
- 9-10: Comprehensive review or seminal study directly detailing {pollutant}'s toxicity mechanisms and effects relevant to the context.
- 7-8: Provides strong mechanistic insights, epidemiological data, or specific effects of {pollutant} on {target_tissue} or related phenotypes.
- 5-6: Discusses general toxicity of {pollutant} or related compounds, potentially relevant but not specific to the context.
- 3-4: Mentions {pollutant} or toxicity tangentially; low direct relevance to mechanisms or target tissue/phenotypes.
- 1-2: Not relevant to {pollutant} toxicity background.

[Required JSON Output]
Return a JSON object containing two lists: 'scores' and 'reasons'.
{{
  "scores": [score_for_article_1, score_for_article_2, ...],
  "reasons": ["Reason for score 1...", "Reason for score 2...", ...]
}}"""
            
            user_prompt = f"""
[Articles to Score]
{chr(10).join(articles_summary)}
"""

            try:
                response = await self.ai_service.generate(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    is_json=True
                )
                

                scores = response.get('scores', [5] * len(batch))
                reasons = response.get('reasons', ['No reason provided'] * len(batch))

                if len(scores) != len(batch) or len(reasons) != len(batch):
                     self._log(f"   AI response length mismatch (scores: {len(scores)}, reasons: {len(reasons)}, expected: {len(batch)}). Using default scores.", 'warn')
                     scores = [5] * len(batch)
                     reasons = ['AI response error'] * len(batch)

                for idx, article in enumerate(batch):
                    score = scores[idx] if isinstance(scores[idx], (int, float)) else 5
                    reason = reasons[idx] if isinstance(reasons[idx], str) else 'Invalid reason type'

                    article['ai_relevance_score'] = score
                    article['ai_relevance_reason'] = reason
                    citation_score = self._normalize_citation_score(article.get('citationCount', 0))
                    article['final_score'] = (score * 0.7) + (citation_score * 0.3)
                    scored_articles.append(article)

                self._log(f"   Scored batch {i//batch_size + 1}: {i+len(batch)}/{len(articles)}")

            except Exception as e:
                self._log(f"   AI scoring failed for batch {i//batch_size + 1}: {e}", 'warn')
                logger.error(f"AI scoring failed for pollutant background batch", exc_info=True)
                for article in batch:
                    article['ai_relevance_score'] = 5
                    article['ai_relevance_reason'] = 'AI scoring failed'
                    citation_score = self._normalize_citation_score(article.get('citationCount', 0))
                    article['final_score'] = (5 * 0.7) + (citation_score * 0.3)
                    scored_articles.append(article)

        filtered_articles = [a for a in scored_articles if a.get('ai_relevance_score', 0) >= 4]
        filtered_articles.sort(key=lambda x: (x.get('final_score', 0), -int(x.get('year', '0')) if str(x.get('year','0')).isdigit() else 0 ), reverse=True)

        self._log(f"Pollutant background scoring complete. Returning top {min(limit, len(filtered_articles))} articles.", 'success')
        return filtered_articles[:limit]


    async def _score_and_filter_articles(
        self,
        articles: List[Dict],
        protein: str,
        pollutant: str,
        target_tissue: str,
        limit: int
    ) -> List[Dict]:
        """AI relevance scoring and filtering for Stage 1 articles"""
        self._log(f"AI Relevance Scoring for {protein}...")
        self._log(f"   Articles to score: {len(articles)}")

        if not articles:
            return []

        batch_size = 10
        scored_articles = []

        for i in range(0, len(articles), batch_size):
            batch = articles[i:i+batch_size]

            articles_summary = []
            for idx, article in enumerate(batch):
                summary = (
                    f"[{idx+1}] PMID:{article.get('pmid', 'N/A')} ({article.get('year', 'N/A')}) "
                    f"Type: {article.get('literature_type', 'unknown')}\n"
                    f"Title: {article.get('title', 'N/A')}\n"
                    f"Abstract: {article.get('abstract', 'N/A')[:300]}...\n"
                )
                articles_summary.append(summary)


            system_prompt = f"""As a biomedical literature reviewer, assess the relevance of these articles to the research topic involving {protein} and {pollutant}.
{LANGUAGE_CONSTRAINT}
[Research Topic]
- Protein of Interest: {protein}
- Pollutant: {pollutant}
- Target Tissue: {target_tissue}
- Goal: Understand how {pollutant} binding to {protein} might lead to toxic effects in {target_tissue}. Focus on FUNCTIONAL consequences.

[Scoring Criteria] (Rate each article from 1 to 10)
- 9-10: Directly studies the functional impact of {pollutant} interacting with {protein}, highly relevant to the goal.
- 7-8: Studies {protein}'s function in a relevant context or studies {pollutant}'s effect on {protein} or related pathways.
- 5-6: Describes {protein}'s general function or {pollutant}'s general toxicity, potentially relevant but connection is indirect.
- 3-4: Mentions {protein} or {pollutant} but in an unrelated context.
- 1-2: Not relevant.

[Required JSON Output]
Return a JSON object containing two lists: 'scores' and 'reasons'.
{{
  "scores": [score_for_article_1, score_for_article_2, ...],
  "reasons": ["Reason for score 1...", "Reason for score 2...", ...]
}}"""

            user_prompt = f"""
[Articles to Score]
{chr(10).join(articles_summary)}
"""

            try:
                response = await self.ai_service.generate(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    is_json=True
                )
                
                
                scores = response.get('scores', [5] * len(batch))
                reasons = response.get('reasons', ['No reason provided'] * len(batch))

                if len(scores) != len(batch) or len(reasons) != len(batch):
                    self._log(f"   AI response length mismatch (scores: {len(scores)}, reasons: {len(reasons)}, expected: {len(batch)}). Using default scores.", 'warn')
                    scores = [5] * len(batch)
                    reasons = ['AI response error'] * len(batch)

                for idx, article in enumerate(batch):
                    score = scores[idx] if isinstance(scores[idx], (int, float)) else 5
                    reason = reasons[idx] if isinstance(reasons[idx], str) else 'Invalid reason type'

                    article['ai_relevance_score'] = score
                    article['ai_relevance_reason'] = reason
                    citation_score = self._normalize_citation_score(article.get('citationCount', 0))
                    article['final_score'] = (score * 0.7) + (citation_score * 0.3)
                    scored_articles.append(article)

                self._log(f"   Scored batch {i//batch_size + 1}: {i+len(batch)}/{len(articles)}")

            except Exception as e:
                self._log(f"   AI scoring failed for batch {i//batch_size + 1}: {e}", 'warn')
                logger.error(f"AI scoring failed for {protein} batch", exc_info=True)
                for article in batch:
                    article['ai_relevance_score'] = 5
                    article['ai_relevance_reason'] = 'AI scoring failed'
                    citation_score = self._normalize_citation_score(article.get('citationCount', 0))
                    article['final_score'] = (5 * 0.7) + (citation_score * 0.3)
                    scored_articles.append(article)

        MIN_ARTICLES_PER_PROTEIN = 3
        IDEAL_THRESHOLD = 4.0
        FALLBACK_THRESHOLD = 3.0
        MINIMUM_THRESHOLD = 2.5
        
        filtered_articles = [a for a in scored_articles if a.get('ai_relevance_score', 0) >= IDEAL_THRESHOLD]
        
        if len(filtered_articles) < MIN_ARTICLES_PER_PROTEIN:
            self._log(f"   Only {len(filtered_articles)} articles meet threshold {IDEAL_THRESHOLD}, lowering to {FALLBACK_THRESHOLD}...", 'warn')
            filtered_articles = [a for a in scored_articles if a.get('ai_relevance_score', 0) >= FALLBACK_THRESHOLD]
        
        if len(filtered_articles) < MIN_ARTICLES_PER_PROTEIN:
            self._log(f"   Only {len(filtered_articles)} articles meet threshold {FALLBACK_THRESHOLD}, lowering to {MINIMUM_THRESHOLD}...", 'warn')
            filtered_articles = [a for a in scored_articles if a.get('ai_relevance_score', 0) >= MINIMUM_THRESHOLD]
        
        if len(filtered_articles) < MIN_ARTICLES_PER_PROTEIN and scored_articles:
            self._log(f"   Still only {len(filtered_articles)} articles, keeping top {MIN_ARTICLES_PER_PROTEIN} by score...", 'warn')
            sorted_articles = sorted(scored_articles, key=lambda x: x.get('ai_relevance_score', 0), reverse=True)
            filtered_articles = sorted_articles[:MIN_ARTICLES_PER_PROTEIN]
        
        filtered_articles.sort(
            key=lambda x: (
                x.get('final_score', 0), 
                -int(x.get('year', '0')) if str(x.get('year','0')).isdigit() else 0
            ), 
            reverse=True
        )
        
        score_range = ""
        if filtered_articles:
            min_score = min([a.get('ai_relevance_score', 0) for a in filtered_articles])
            max_score = max([a.get('ai_relevance_score', 0) for a in filtered_articles])
            score_range = f"Score range: {min_score:.1f}-{max_score:.1f}"
        
        self._log(
            f"   Scoring complete: {len(filtered_articles)} relevant articles identified for {protein}. {score_range}", 
            'success'
        )
        
        return filtered_articles[:int(limit * 1.5)]


    async def _score_targeted_articles(
        self,
        articles: List[Dict],
        protein: str,
        pollutant: str,
        gaps: List[str],
        critiques: List[str]
    ) -> List[Dict]:
        """Score Stage 2 articles based on how well they address gaps/critiques."""
        self._log(f"Scoring Stage 2 targeted articles for {protein}...")

        if not articles:
            return []

        batch_size = 10
        scored_articles = []

        gap_critique_context = "[Knowledge Gaps to Address]\n" + "\n".join([f"- {g}" for g in gaps[:5]])
        gap_critique_context += "\n\n[Critiques/Questions to Address]\n" + "\n".join([f"- {c}" for c in critiques[:5]])

        for i in range(0, len(articles), batch_size):
            batch = articles[i:i+batch_size]

            articles_summary = []
            for idx, article in enumerate(batch):
                addressed_context = article.get('addresses_gap', 'General targeted search')
                summary = (
                    f"[{idx+1}] PMID:{article.get('pmid','N/A')} ({article.get('year', 'N/A')})\n"
                    f"   Addresses: {addressed_context[:100]}...\n"
                    f"   Title: {article.get('title', 'N/A')}\n"
                    f"   Abstract: {article.get('abstract', 'N/A')[:300]}...\n"
                )
                articles_summary.append(summary)

            
            system_prompt = f"""Evaluate how well these targeted articles address specific knowledge gaps or critiques regarding the role of {protein} in {pollutant} toxicity.
{LANGUAGE_CONSTRAINT}
{gap_critique_context}

[Scoring Criteria] (Rate each article from 1 to 10)
- 9-10: Directly and substantially addresses a specified gap/critique.
- 7-8: Provides relevant information that partially addresses a gap/critique.
- 5-6: Contains potentially useful but not direct information.
- 3-4: Tangentially related.
- 1-2: Not relevant.

[Required JSON Output]
Return a JSON object with 'scores' and 'gap_resolution'.
{{
  "scores": [score1, score2, ...],
  "gap_resolution": ["Explanation for article 1...", "Explanation for article 2...", ...]
}}"""

            user_prompt = f"""
[Articles to Evaluate]
{chr(10).join(articles_summary)}
"""

            try:
                response = await self.ai_service.generate(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    is_json=True
                )
                
                
                scores = response.get('scores', [5] * len(batch))
                gap_resolutions = response.get('gap_resolution', ['No resolution explanation provided'] * len(batch))

                if len(scores) != len(batch) or len(gap_resolutions) != len(batch):
                    self._log(f"   AI response length mismatch (scores: {len(scores)}, resolutions: {len(gap_resolutions)}, expected: {len(batch)}). Using defaults.", 'warn')
                    scores = [5] * len(batch)
                    gap_resolutions = ['AI response error'] * len(batch)

                for idx, article in enumerate(batch):
                    score = scores[idx] if isinstance(scores[idx], (int, float)) else 5
                    resolution = gap_resolutions[idx] if isinstance(gap_resolutions[idx], str) else 'Invalid resolution type'

                    article['ai_relevance_score'] = score
                    article['gap_resolution'] = resolution
                    citation_score = self._normalize_citation_score(article.get('citationCount', 0))
                    article['final_score'] = (score * 0.7) + (citation_score * 0.3)
                    scored_articles.append(article)

                self._log(f"   Scored Stage 2 batch {i//batch_size + 1}: {i+len(batch)}/{len(articles)}")

            except Exception as e:
                self._log(f"   AI scoring failed for Stage 2 batch {i//batch_size + 1}: {e}", 'warn')
                logger.error(f"AI scoring failed for Stage 2 {protein} batch", exc_info=True)
                for article in batch:
                    article['ai_relevance_score'] = 5
                    article['gap_resolution'] = 'AI scoring failed'
                    citation_score = self._normalize_citation_score(article.get('citationCount', 0))
                    article['final_score'] = (5 * 0.7) + (citation_score * 0.3)
                    scored_articles.append(article)

        scored_articles.sort(key=lambda x: x.get('final_score', 0), reverse=True)

        self._log(f"Stage 2 scoring complete for {protein}. {len(scored_articles)} articles scored.", 'success')
        return scored_articles


    async def _generate_targeted_queries(
        self,
        protein: str,
        pollutant: str,
        target_tissue: str,
        gaps: List[str],
        critiques: List[str]
    ) -> List[Dict]:
        """Generate targeted PubMed search queries using AI to address gaps/critiques."""
        self._log(f"Generating targeted queries for {protein}...", 'info')

        gaps_text = '\n'.join([f"- {g}" for g in gaps[:3]])
        critiques_text = '\n'.join([f"- {c}" for c in critiques[:3]])

        
        system_prompt = f"""You are a research librarian. You will generate specific, targeted PubMed search queries to find literature addressing the knowledge gaps and critiques listed below regarding the role of {protein} in {pollutant} toxicity in {target_tissue}.
{LANGUAGE_CONSTRAINT}
[Context]
- Protein: {protein}
- Pollutant: {pollutant}
- Target Tissue: {target_tissue}

[Required JSON Output Format]
Return ONLY a JSON object with a single key "queries". Each query object MUST contain 'purpose', 'query', 'addresses_gap', 'limit', and 'type' fields.
{{
  "queries": [
    {{
      "purpose": "Brief description of what THIS specific query aims to find.",
      "query": "A precise PubMed search string.",
      "addresses_gap": "Copy the EXACT text of the specific gap or critique this query is designed to address.",
      "limit": 5,
      "type": "targeted"
    }}
  ]
}}

[Instructions & Constraints]
- Create focused queries.
- Use PubMed syntax (e.g., [MeSH Terms], [Publication Type], AND, OR, NOT).
- Ensure the 'query' string is directly aimed at the 'addresses_gap' text.
- All text MUST be in English."""
        
        user_prompt = f"""
[Knowledge Gaps to Address]
{gaps_text if gaps else "No specific gaps provided."}

[Critiques/Questions to Address]
{critiques_text if critiques else "No specific critiques provided."}

[Task]
Generate 2-4 highly specific PubMed search queries.
"""

        try:
            response = await self.ai_service.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                is_json=True
            )
            

            if not response or not isinstance(response, dict) or 'queries' not in response:
                self._log("  AI response missing 'queries' field.", 'error')
                return []

            queries_list = response['queries']
            if not isinstance(queries_list, list):
                self._log("  AI response 'queries' field is not a list.", 'error')
                return []

            validated_queries = []
            for idx, q_data in enumerate(queries_list):
                if not isinstance(q_data, dict):
                    self._log(f"  Query item {idx+1} is not a dictionary.", 'warn')
                    continue

                required_fields = ['purpose', 'query', 'addresses_gap']
                missing_or_empty = [f for f in required_fields if f not in q_data or not isinstance(q_data[f], str) or not q_data[f].strip()]
                if missing_or_empty:
                    self._log(f"  Query item {idx+1} is missing or has empty required fields: {', '.join(missing_or_empty)}.", 'warn')
                    continue

                if len(q_data['query'].strip()) < 10:
                    self._log(f"  Query item {idx+1} has an unusually short query string: '{q_data['query']}'.", 'warn')
                    continue

                validated_query = {
                    'name': f"Targeted: {q_data['purpose'][:40]}...",
                    'purpose': q_data['purpose'].strip(),
                    'query': q_data['query'].strip(),
                    'addresses_gap': q_data['addresses_gap'].strip(),
                    'limit': q_data.get('limit', 5) if isinstance(q_data.get('limit'), int) else 5,
                    'type': 'targeted'
                }
                validated_queries.append(validated_query)
                self._log(f"  Generated Query {idx+1}: {validated_query['purpose'][:60]}...", 'info')

            if not validated_queries:
                self._log("  AI generated response, but no valid queries were extracted.", 'warn')
                return []

            self._log(f"Generated {len(validated_queries)} targeted search queries successfully.", 'success')
            return validated_queries

        except Exception as e:
            self._log(f"Targeted query generation failed: {e}", 'error')
            logger.error(f"AI query generation failed for {protein}", exc_info=True)
            return []



    async def _search_protein_function(
        self,
        protein: str,
        target_tissue: str,
        limit: int,
        start_year: str,
        end_year: str,
        email: str,
        broader: bool = False,
        epmc_filters: Dict = None
    ) -> List[Dict]:
        """Search protein function literature"""
        epmc_filters = epmc_filters or {}
        
        protein_query_terms = await self._build_alias_query_string(protein)
        
        function_keywords = await self._generate_function_keywords(protein)
        
        mesh_terms = await self.bio_service.get_mesh_terms(protein)
        
        search_strategies = []
        
        query1 = f'({protein_query_terms}) AND ("{target_tissue}") AND (function OR role OR activity)'
        if mesh_terms:
            mesh_query = " OR ".join([f'"{term}"[MeSH Terms]' for term in mesh_terms])
            query1 += f' OR ({mesh_query})'
        search_strategies.append({
            'name': f'{protein} Function in {target_tissue}',
            'query': query1,
            'priority': 'high',
            'type': 'function',
            'limit': int(limit * 0.4)
        })
        
        if function_keywords.get('biological_processes'):
            process_terms = ' OR '.join([f'"{p}"' for p in function_keywords['biological_processes'][:3]])
            search_strategies.append({
                'name': f'{protein} Biological Processes',
                'query': f'({protein_query_terms}) AND ({process_terms})',
                'priority': 'medium',
                'type': 'function',
                'limit': int(limit * 0.3)
            })
        
        search_strategies.append({
            'name': f'{protein} Signaling Pathways',
            'query': f'({protein_query_terms}) AND ("signaling pathway" OR "signal transduction" OR pathway)',
            'priority': 'medium',
            'type': 'function',
            'limit': int(limit * 0.3)
        })

        if broader:
            search_strategies.append({
                'name': f'Broad {protein} Function',
                'query': f'({protein_query_terms}[Title/Abstract]) AND (review[Publication Type] OR function[Title/Abstract])',
                'priority': 'low',
                'type': 'function',
                'limit': int(limit * 0.5)
            })
        
        all_articles = await self._execute_search_strategies(
            search_strategies, start_year, end_year, epmc_filters, email
        )
        
        if len(all_articles) < limit * 0.5 and not broader:
            self._log(f"   {protein}: Too few results ({len(all_articles)}), broadening search...", 'warn')
            
            fallback_query = {
                'name': f'{protein} Fallback',
                'query': f'({protein_query_terms})',
                'priority': 'low',
                'type': 'function',
                'limit': limit
            }
            
            extra = await self._execute_search_strategies(
                [fallback_query], start_year, end_year, epmc_filters, email
            )
            all_articles.extend(extra)
        
        return all_articles


    async def _search_protein_pollutant_interaction(
        self,
        protein: str,
        pollutant: str,
        target_tissue: str,
        target_phenotypes: List[str],
        limit: int,
        start_year: str,
        end_year: str,
        email: str,
        epmc_filters: Dict = None
    ) -> List[Dict]:
        """Search protein-pollutant interaction literature"""
        epmc_filters = epmc_filters or {}
        search_strategies = []
        
        protein_query_terms = await self._build_alias_query_string(protein)

        search_strategies.append({
            'name': f'{protein}-{pollutant} Core Interaction',
            'query': f'({protein_query_terms}) AND ("{pollutant}") AND (interaction OR binding OR toxicity)',
            'priority': 'high',
            'type': 'interaction',
            'limit': int(limit * 0.4)
        })
        
        if target_phenotypes:
            phenotype_terms = ' OR '.join([f'"{p}"' for p in target_phenotypes[:2]])
            search_strategies.append({
                'name': f'{pollutant} Phenotype + {protein}',
                'query': f'("{pollutant}") AND ({phenotype_terms}) AND ({protein_query_terms})',
                'priority': 'high',
                'type': 'interaction',
                'limit': int(limit * 0.3)
            })
        
        search_strategies.append({
            'name': f'{pollutant} in {target_tissue} + {protein}',
            'query': f'("{pollutant}") AND ("{target_tissue}") AND ({protein_query_terms})',
            'priority': 'medium',
            'type': 'interaction',
            'limit': int(limit * 0.3)
        })
        
        all_articles = await self._execute_search_strategies(
            search_strategies, start_year, end_year, epmc_filters, email
        )

        return all_articles


    async def _generate_function_keywords(self, protein: str) -> Dict[str, List[str]]:
        """Generate protein function keywords via AI"""
        self._log(f"   Generating function keywords for {protein} using AI...", 'info')

        
        system_prompt_content = f"""As a molecular biology expert, you must provide accurate functional information.
{LANGUAGE_CONSTRAINT}
Focus on the most well-established and critical roles."""

        user_prompt_content = f"""List the top 5 most important biological processes and top 5 molecular functions specifically associated with the protein {protein}.

Return the result STRICTLY in the following JSON format:
{{
    "biological_processes": ["Process 1", "Process 2", ...],
    "molecular_functions": ["Function 1", "Function 2", ...]
}}"""

        try:
            response = await self.ai_service.generate(
                system_prompt=system_prompt_content,
                user_prompt=user_prompt_content,
                is_json=True
            )
            
            
            if isinstance(response, dict) and \
               isinstance(response.get('biological_processes'), list) and \
               isinstance(response.get('molecular_functions'), list):
               self._log(f"   ✓ Keywords generated for {protein}", 'success')
               return response
            else:
                self._log(f"   ✗ Invalid format received for {protein} keywords.", 'warn')
                return {'biological_processes': [], 'molecular_functions': []}
        except Exception as e:
            self._log(f"   AI keyword generation failed for {protein}: {e}", 'warn')
            logger.error(f"AI keyword generation failed for {protein}", exc_info=True)
            return {'biological_processes': [], 'molecular_functions': []}


    # --- V3 REPAIR: START (Step 3) ---
    # Updated prompt with V3 fields
    def _create_pollutant_extraction_prompt(self, text: str, pollutant: str) -> str:
        """Create prompt for extracting pollutant background info."""
        return f"""As a senior toxicologist, extract key information specifically about {pollutant} from the provided article text.
{LANGUAGE_CONSTRAINT}
[Article Text]
{text}

[Extraction Tasks]
1.  **Toxic Effects:** List the primary adverse health effects.
2.  **Molecular Mechanisms:** Identify specific molecular mechanisms of toxicity.
3.  **Target Organs/Tissues/Cells:** List the primary biological targets.
4.  **Key Pathways:** Identify major signaling pathways disrupted.

[Required JSON Output Format (V3)]
Return ONLY a JSON object. Use empty arrays `[]` or "Not specified" if information is not found.
{{
  "toxic_effects": ["Effect 1"],
  "molecular_mechanisms": ["Mechanism 1"],
  "target_organs": ["Organ 1"],
  "key_pathways": ["Pathway 1"],
  "functional_link_to_phenotype": ["【MUST ANSWER】From 'Results'/'Discussion', extract evidence linking {pollutant} to the target phenotype (e.g., hepatotoxicity, steatosis)."],
  "pollutant_impact_on_function": ["【BONUS/OPTIONAL】Does this text mention {pollutant}'s effect on a *specific protein*? If so, what? Or 'Not mentioned'."],
  "key_phenotype_results_data": ["Extract 1-2 key quantitative/qualitative results from the 'Results' section related to the phenotype."],
  "author_conclusions_on_phenotype": ["Summarize the mechanism hypothesis from the 'Discussion'/'Conclusion' section."],
  "evidence_strength": "strong / moderate / weak / speculative"
}}"""

    # --- V3 REPAIR: START (Step 3) ---
    # Updated prompt with V3 fields
    def _create_protein_function_extraction_prompt(self, text: str, protein: str) -> str:
        """Create prompt for extracting protein function info."""
        return f"""Extract key functional information about the protein {protein} from the provided article text.
{LANGUAGE_CONSTRAINT}
[Article Text]
{text}

[Extraction Tasks]
1.  **Primary Functions:** List the main known biological functions.
2.  **Tissue/Cellular Localization:** Mention where {protein} is primarily expressed.
3.  **Biological Pathways:** Identify the key metabolic or signaling pathways.
4.  **Molecular Interactions:** List important substrates, binding partners, etc.

[Required JSON Output Format (V3)]
Return ONLY a JSON object. Use empty arrays `[]` or "Not specified" if information is not found.
{{
  "primary_functions": ["Function 1"],
  "localization": ["Tissue 1"],
  "pathways": ["Pathway 1 Name"],
  "molecular_interactions": ["Substrate/Partner 1"],
  "functional_link_to_phenotype": ["【MUST ANSWER】From 'Results'/'Discussion', extract evidence linking this protein's function to the target phenotype (e.g., hepatotoxicity, steatosis)."],
  "pollutant_impact_on_function": ["【BONUS/OPTIONAL】Does this text mention a *pollutant's* effect on {protein}? If so, what? Or 'Not mentioned'."],
  "key_phenotype_results_data": ["Extract 1-2 key results from the 'Results' section related to the protein's function and the phenotype."],
  "author_conclusions_on_phenotype": ["Summarize the mechanism hypothesis from the 'Discussion'/'Conclusion' section linking function to phenotype."],
  "evidence_strength": "strong / moderate / weak / speculative"
}}"""

    # --- V3 REPAIR: START (Step 3) ---
    # Updated prompt with V3 fields
    def _create_interaction_extraction_prompt(self, text: str, protein: str, pollutant: str) -> str:
        """Create prompt for extracting protein-pollutant interaction info."""
        return f"""Analyze the provided article text for evidence relevant to {protein} and {pollutant}.
{LANGUAGE_CONSTRAINT}
[Article Text]
{text}

[Extraction Tasks]
1.  **Functional-Phenotype Link:** What evidence links {protein}'s function to the toxic phenotype (e.g., hepatotoxicity)?
2.  **Pollutant Impact (Bonus):** Is an interaction/effect between {pollutant} and {protein} described?
3.  **Key Phenotype Results:** Extract key quantitative/qualitative data from 'Results' linking {protein} function to the phenotype.
4.  **Author Conclusions:** Summarize mechanism hypotheses from 'Discussion' about the phenotype link.

[Required JSON Output Format (V3)]
Return ONLY a JSON object. Use "Not specified" or placeholders if information is absent.
{{
  "functional_link_to_phenotype": ["【MUST ANSWER】From 'Results'/'Discussion', extract evidence linking this protein's function to the target phenotype."],
  "pollutant_impact_on_function": ["【BONUS/OPTIONAL】Does this text mention {pollutant}'s effect on {protein}? If so, what? (e.g., 'Direct binding', 'Inhibition'). Or 'Not mentioned'."],
  "key_phenotype_results_data": ["Extract 1-2 key results from the 'Results' section related to the protein's function and the phenotype."],
  "author_conclusions_on_phenotype": ["Summarize the mechanism hypothesis from the 'Discussion'/'Conclusion' section linking function to phenotype."],
  "evidence_strength": "Assess the strength of the *protein-phenotype link* (strong / moderate / weak / speculative)."
}}"""
    # --- V3 REPAIR: END (Step 3) ---

    def _deduplicate_and_finalize(self, articles: List[Dict], limit: int) -> List[Dict]:
        """Deduplicate articles based on PMID/DOI/Title and sort by final_score."""
        seen_keys = set()
        unique_articles = []

        articles.sort(key=lambda x: x.get('final_score', 0), reverse=True)

        for article in articles:
            key = self.bio_service.literature_coordinator._get_article_key(article)

            if key and key not in seen_keys:
                unique_articles.append(article)
                seen_keys.add(key)
            elif not key:
                 abstract_start = article.get('abstract', '')[:50]
                 fallback_key = f"abstract:{abstract_start}"
                 if abstract_start and fallback_key not in seen_keys:
                     unique_articles.append(article)
                     seen_keys.add(fallback_key)


        unique_articles.sort(key=lambda x: (
            x.get('final_score', 0),
            -int(x.get('year', '0')) if str(x.get('year','0')).isdigit() else 0
        ), reverse=True)

        return unique_articles[:limit]

    def _generate_quality_metrics(self, articles: List[Dict]) -> Dict:
        """Generate objective quality metrics for a list of articles."""
        if not articles:
            return {
                'total_articles': 0, 'avg_relevance_score': 0, 'high_quality_count': 0,
                'has_pmcid_count': 0, 'has_fulltext_count': 0, 'avg_year': 0,
                'year_range': [None, None], 'avg_citations': 0
            }

        scores = [a.get('ai_relevance_score', 5) for a in articles]
        years = [int(y) for a in articles if (y := a.get('year')) and str(y).isdigit()]
        citations = [c for a in articles if (c := a.get('citationCount')) is not None and isinstance(c, int)]

        year_range = [min(years), max(years)] if years else [None, None]

        return {
            'total_articles': len(articles),
            'avg_relevance_score': round(sum(scores) / len(scores), 1) if scores else 0,
            'high_quality_count': sum(1 for s in scores if s >= 7),
            'has_pmcid_count': sum(1 for a in articles if a.get('pmcid')),
            'has_fulltext_count': sum(1 for a in articles if a.get('full_text')),
            'avg_year': round(sum(years) / len(years)) if years else 0,
            'year_range': year_range,
            'avg_citations': round(sum(citations) / len(citations), 1) if citations else 0
        }

    def _normalize_citation_score(self, citation_count: Optional[int]) -> float:
        """Normalize citation count to a 0-10 scale using logarithm."""
        if citation_count is None or not isinstance(citation_count, int) or citation_count < 0:
            return 0.0
        if citation_count == 0:
            return 0.0

        score = math.log1p(citation_count) * 1.5
        return min(round(score, 1), 10.0)


# This class is not used by SmartLiteratureSearcher, but was part of the original file.
# V3 REPAIR: We are copying _merge_extraction_results from here into SmartLiteratureSearcher.
# The rest of this class remains unused, as per the original file structure.
class LiteratureExtractor:
    """Enhanced with chunking support"""
    
    def __init__(self, ai_service):
        self.ai_service = ai_service
        self.estimate_tokens = estimate_tokens
        self.split_text = split_text_into_chunks

    async def _extract_direct(self, text: str, extraction_type: str, context: Dict) -> Dict:
        # This is a placeholder for the direct extraction logic.
        # In a real implementation, this would call the AI service
        # with a specific prompt for extraction on the given text.
        logger.info(f"Performing direct extraction for type '{extraction_type}'...")
        # Simulating an AI call for demonstration purposes.
        await asyncio.sleep(0.1) 
        return {
            "pmid": context.get("pmid", "Unknown"),
            "title": context.get("title", "Unknown"),
            "key_findings": [f"Mock finding from text of length {len(text)}"],
            "molecular_mechanisms": ["Mock mechanism"],
        }
    
    async def extract_with_chunking(
        self, 
        full_text: str, 
        extraction_type: str,
        context: Dict
    ) -> Dict:
        """Extract from long text using chunking if needed"""
        
        text_tokens = self.estimate_tokens(full_text)
        safe_limit = 30000
        
        if text_tokens <= safe_limit:
            return await self._extract_direct(full_text, extraction_type, context)
        
        logger.info(
            f"Text too long ({text_tokens} tokens). Splitting into chunks..."
        )
        
        chunks = self.split_text(full_text, max_tokens_per_chunk=25000)
        logger.info(f"Split into {len(chunks)} chunks")
        
        chunk_results = []
        for i, chunk in enumerate(chunks):
            logger.info(f"Processing chunk {i+1}/{len(chunks)}...")
            try:
                result = await self._extract_direct(chunk, extraction_type, context)
                chunk_results.append(result)
            except Exception as e:
                logger.error(f"Chunk {i+1} extraction failed: {e}")
                continue
        
        merged = self._merge_extraction_results(chunk_results, extraction_type)
        return merged
    
    def _merge_extraction_results(self, results: List[Dict], extraction_type: str) -> Dict:
        """Merge results from multiple chunks"""
        if not results:
            return {}
        
        if len(results) == 1:
            return results[0]
        
        merged = {
            'pmid': results[0].get('pmid'),
            'title': results[0].get('title'),
            'extraction_type': extraction_type
        }
        
        for key in ['toxic_effects', 'molecular_mechanisms', 'key_pathways', 
                    'key_findings', 'primary_functions', 'pathways']:
            all_items = []
            for r in results:
                if key in r and isinstance(r[key], list):
                    all_items.extend(r[key])
            merged[key] = list(dict.fromkeys(all_items))[:10]
        
        for key in ['functional_impact', 'evidence_summary']:
            parts = [r.get(key, '') for r in results if r.get(key)]
            if parts:
                merged[key] = ' '.join(parts)[:500]
        
        return merged


if __name__ == "__main__":
    async def test():
        print("\n" + "="*70)
        print("TESTING LITERATURE SERVICES (Including SmartSearcher)")
        print("="*70)

        print("\nTest 1: Europe PMC alone (Existing Functionality)")
        epmc = EuropePMCService()
        articles_epmc = await epmc.search("PFOA liver toxicity", limit=5, open_access_only=True)
        print(f"  Result: {len(articles_epmc)} articles")
        if articles_epmc:
            print(f"  First: {articles_epmc[0]['title'][:60]}...")
        else:
            print(f"  FAILED or No Results")

        print("\nTest 2: Coordinator (PubMed + Europe PMC) (Existing Functionality)")
        pubmed = PubMedService(email="test@example.com")
        coordinator = LiteratureSearchCoordinator(pubmed, epmc)
        merged, stats = await coordinator.dual_search("PFOA liver toxicity", limit=10, open_access_only=True)
        print(f"  PubMed: {stats['pubmed_count']}")
        print(f"  Europe PMC: {stats['epmc_count']}")
        print(f"  Merged: {stats['merged_count']}")
        if stats['epmc_count'] > 0:
            print(f"  Coordinator test appears successful.")
        else:
            print(f"  Coordinator test - Europe PMC might still have issues.")

        print("\nTest 3: SmartLiteratureSearcher (New - Requires Mocks or API Key)")
        try:
            class MockAIService:
                 async def generate(self, system_prompt, user_prompt, is_json=False):
                     if is_json:
                         if "scores" in system_prompt: return {"scores": [7]*5, "reasons": ["Mock reason"]*5}
                         if "aliases" in user_prompt: return {"aliases": [re.search(r'\"(.*?)\"', user_prompt).group(1), "PXR", "NR1I2"]}
                         if "keywords" in user_prompt: return {"biological_processes": ["mock process"], "molecular_functions": ["mock function"]}
                         if "extract" in user_prompt: return {"pmid": "123", "key_findings": ["mock finding"]}
                         if "queries" in system_prompt: return {"queries": [{"purpose": "mock", "query": "mock query", "addresses_gap": "mock gap", "limit": 1, "type": "targeted"}]}
                     return "Mock AI text response"

            class MockBioService:
                 literature_coordinator = coordinator
                 async def normalize_protein_name(self, protein_input, species_taxon): return {'standard_name': protein_input.upper()}
                 async def get_mesh_terms(self, protein, species_taxon=9606): return ["mock mesh term"]
                 async def get_pdf_full_text(self, article, email): return ("Mock full text", b"mock_pdf_bytes")
                 async def fetch_articles_by_pmids(self, pmids, email): return []
                 async def get_citation_network(self, pmids, email): return []

            mock_bio_service = MockBioService()
            mock_ai_service = MockAIService()
            mock_rate_limiter = None

            searcher = SmartLiteratureSearcher(mock_bio_service, mock_ai_service, mock_rate_limiter)

            print("  Testing smart_search_stage1...")
            stage1_result = await searcher.smart_search_stage1(
                 protein="ALB", pollutant="PFOA", target_tissue="Liver",
                 target_phenotypes=["hepatotoxicity"], email="test@example.com", limit=5
            )
            print(f"  Stage 1 Result: Found {len(stage1_result.get('articles', []))} articles.")
            if stage1_result.get('articles'):
                 print(f"    First article score: {stage1_result['articles'][0].get('final_score', 'N/A')}")
            else:
                 print("    Stage 1 search returned no articles (check mocks or API limits).")

        except Exception as e:
             print(f"  SmartSearcher test failed: {e}")
             print("  (This might be expected if mocks/API keys are not set up)")


        print("\n" + "="*70)

    logging.basicConfig(level=logging.INFO)
    asyncio.run(test())