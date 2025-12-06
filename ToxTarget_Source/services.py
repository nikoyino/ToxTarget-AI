# -*- coding: utf-8 -*-
"""
External Services Module
Includes: Database Integration (KEGG, UniProt, GTEx, HPA), AI Client
"""

import aiohttp
import asyncio
import json
import logging
import random
import re
import os
import csv
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential
from utils import PersistentCache, RateLimiter

from literature_services import PubMedService, EuropePMCService, LiteratureSearchCoordinator

try:
    import fitz
except ImportError:
    fitz = None

logger = logging.getLogger(__name__)


class BioDatabaseService:
    """Biological Database Integration Service"""

    def __init__(self, proxy: str = None, timeout: int = 30, progress_callback=None, email: str = "default@example.com"):
        self.cache = PersistentCache('biodb_cache.db')
        self.rate_limiter = RateLimiter()
        self.ncbi_base = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils'
        self.epmc_base = 'https://www.ebi.ac.uk/europepmc/webservices/rest'

        self.proxy = proxy
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.progress_callback = progress_callback

        pubmed_service = PubMedService(email=email)
        epmc_service = EuropePMCService()
        self.literature_coordinator = LiteratureSearchCoordinator(pubmed_service, epmc_service)

        if fitz is None:
            logger.warning("PyMuPDF not installed. PDF parsing disabled.")

        self.hpa_tsv_path = 'rna_tissue_consensus.tsv'
        self.hpa_data = None
        self._load_local_hpa_data()

        self.gtex_file_path = 'GTEx_Analysis_v8_gene_median_tpm.gct'
        self.gtex_index = None
        self.gtex_tissues = []
        self._build_gtex_index()

        logger.info(f"Database service initialized (proxy: {self.proxy or 'None'})")

    def _load_local_hpa_data(self):
        """Loads the HPA consensus tsv file into memory."""
        if not os.path.exists(self.hpa_tsv_path):
            message = f"Warning: Local HPA file '{self.hpa_tsv_path}' not found. HPA expression analysis will be skipped."
            logger.warning(message)
            self._report_progress(message)
            self.hpa_data = None
            return

        self._report_progress(f"Loading local HPA data from '{self.hpa_tsv_path}'...")
        try:
            hpa_dict = {}
            with open(self.hpa_tsv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f, delimiter='\t')
                header = next(reader)
                
                try:
                    gene_name_idx = header.index('Gene name')
                    tissue_idx = header.index('Tissue')
                    ntpm_idx = header.index('nTPM')
                except ValueError as e:
                    logger.error(f"Could not find required columns in HPA file: {e}")
                    self.hpa_data = None
                    return

                for row in reader:
                    gene_name = row[gene_name_idx]
                    tissue = row[tissue_idx].lower()
                    ntpm = float(row[ntpm_idx])
                    
                    if gene_name not in hpa_dict:
                        hpa_dict[gene_name] = {}
                    hpa_dict[gene_name][tissue] = ntpm
            
            self.hpa_data = hpa_dict
            message = f"Successfully loaded local HPA data for {len(self.hpa_data)} genes."
            logger.info(message)
            self._report_progress(message)

        except Exception as e:
            message = f"Error loading local HPA data: {e}"
            logger.error(message, exc_info=True)
            self._report_progress(message)
            self.hpa_data = None

    def _build_gtex_index(self):
        """Build gene-to-file-position index for GTEx data (Strategy B)."""
        if not os.path.exists(self.gtex_file_path):
            message = f"Warning: Local GTEx file '{self.gtex_file_path}' not found. GTEx expression analysis will be skipped."
            logger.warning(message)
            self._report_progress(message)
            self.gtex_index = None
            return

        self._report_progress(f"Building GTEx index from '{self.gtex_file_path}'...")
        try:
            gtex_idx = {}
            with open(self.gtex_file_path, 'r', encoding='utf-8') as f:
                f.readline()
                f.readline()
                
                header_line = f.readline()
                header = header_line.strip().split('\t')
                self.gtex_tissues = header[2:]
                
                while True:
                    position = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    
                    parts = line.strip().split('\t')
                    if len(parts) < 2:
                        continue
                    
                    gene_id = parts[0]
                    gene_symbol = parts[1]
                    
                    gtex_idx[gene_symbol] = position
                    gtex_idx[gene_id] = position
            
            self.gtex_index = gtex_idx
            message = f"Successfully built GTEx index for {len(gtex_idx)//2} genes with {len(self.gtex_tissues)} tissues."
            logger.info(message)
            self._report_progress(message)

        except Exception as e:
            message = f"Error building GTEx index: {e}"
            logger.error(message, exc_info=True)
            self._report_progress(message)
            self.gtex_index = None

    def _query_gtex_from_local(self, gene_symbol: str, tissue: str = None) -> Optional[Dict]:
        """Query GTEx expression from local file using index (Strategy B)."""
        if self.gtex_index is None:
            return None
        
        file_position = self.gtex_index.get(gene_symbol)
        if file_position is None:
            logger.debug(f"GTEx (Local): Gene '{gene_symbol}' not found in index.")
            return None
        
        try:
            with open(self.gtex_file_path, 'r', encoding='utf-8') as f:
                f.seek(file_position)
                line = f.readline()
                
                parts = line.strip().split('\t')
                if len(parts) < 3:
                    return None
                
                gene_id = parts[0]
                gene_name = parts[1]
                tpm_values = [float(x) if x != '' else 0.0 for x in parts[2:]]
                
                if tissue:
                    tissue_lower = tissue.lower()
                    tissue_idx = None
                    for idx, t in enumerate(self.gtex_tissues):
                        if t.lower() == tissue_lower:
                            tissue_idx = idx
                            break
                    
                    if tissue_idx is not None and tissue_idx < len(tpm_values):
                        return {
                            'gene': gene_name,
                            'gene_id': gene_id,
                            'tissue': self.gtex_tissues[tissue_idx],
                            'median_tpm': tpm_values[tissue_idx],
                            'unit': 'TPM',
                            'data_source': 'GTEx Portal v8 (Local File)'
                        }
                    else:
                        logger.debug(f"GTEx (Local): Tissue '{tissue}' not found for {gene_symbol}.")
                        return None
                else:
                    tissue_expression = {
                        self.gtex_tissues[i]: tpm_values[i] 
                        for i in range(len(self.gtex_tissues)) if i < len(tpm_values)
                    }
                    return {
                        'gene': gene_name,
                        'gene_id': gene_id,
                        'tissue_expression': tissue_expression,
                        'unit': 'TPM',
                        'data_source': 'GTEx Portal v8 (Local File)'
                    }
        
        except Exception as e:
            logger.error(f"GTEx (Local): Error reading data for {gene_symbol}: {e}")
            return None

    async def get_hpa_expression(self, gene_symbol: str) -> Optional[Dict]:
        """Get HPA expression data (Async wrapper)."""
        if not self.hpa_data:
            return None
        gene_key = gene_symbol.upper()
        if gene_key in self.hpa_data:
            return {
                'gene': gene_symbol,
                'rna_tissue_expression': self.hpa_data[gene_key],
                'data_source': 'Human Protein Atlas (Local)'
            }
        return None

    async def get_gtex_expression(self, gene_symbol: str, tissue: str = None) -> Optional[Dict]:
        """Get GTEx expression data (Async wrapper)."""
        try:
            return self._query_gtex_from_local(gene_symbol, tissue)
        except Exception as e:
            logger.error(f"Error in get_gtex_expression: {e}")
            return None

    def _report_progress(self, message: str):
        if self.progress_callback:
            self.progress_callback(message)
        logger.info(message)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True
    )
    async def _api_call(self, session: aiohttp.ClientSession, url: str, params: Dict = None, data: Dict = None, method: str = 'get', timeout: int = 30, headers: Dict = None) -> Any:
        """Generic API call with retry and 403 handling"""
        request_kwargs = {
            'params': params if method.lower() == 'get' else None,
            'json': data if method.lower() == 'post' else None,
            'timeout': aiohttp.ClientTimeout(total=timeout),
            'headers': headers
        }

        if self.proxy:
            request_kwargs['proxy'] = self.proxy

        for attempt in range(3):
            try:
                async with session.request(method, url, **request_kwargs) as response:
                    if response.status == 403:
                        error_text = await response.text()
                        logger.error(
                            f"API access forbidden (403): {url}\n"
                            f"Error: {error_text[:200]}\n"
                            f"This may indicate rate limiting or IP restrictions."
                        )
                        raise aiohttp.ClientResponseError(
                            request_info=response.request_info,
                            history=response.history,
                            status=403,
                            message=f"API access forbidden: {url}",
                            headers=response.headers
                        )


                    if response.status != 200:
                        error_text = await response.text()
                        logger.warning(f"API failed (Status: {response.status}): {url}. Error: {error_text[:200]}")
                        response.raise_for_status()

                    content_type = response.headers.get('Content-Type', '')
                    if 'application/json' in content_type:
                        return await response.json()
                    elif 'application/pdf' in content_type:
                        return await response.read()
                    return await response.text()

            except aiohttp.ClientConnectorError as e:
                if attempt < 2:
                    wait_time = 2 ** attempt
                    logger.warning(f"Connection failed, retry in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    raise

    async def normalize_protein_name(self, protein_input: str, species_taxon: int = 9606, ai_service: Optional['AIService'] = None) -> Optional[Dict]:
        """Protein name normalization with Search-Verify-LLM_Assist logic."""
        cache_key = f"norm_v3_{species_taxon}_{protein_input}"
        if cached := self.cache.get(cache_key):
            return cached
        
        self._report_progress(f"Normalizing: {protein_input}")
        logger.info(f"Normalizing (Robust): {protein_input} (Species: {species_taxon})")
        
        # 1. Gather Top 5 candidates
        uniprot_candidates = await self._normalize_via_uniprot(protein_input, species_taxon)
        ncbi_candidates = await self._normalize_via_ncbi_gene(protein_input, species_taxon)
        
        all_candidates = uniprot_candidates + ncbi_candidates
        
        seen_names = set()
        candidates = []
        for c in all_candidates:
            if c['standard_name'].upper() not in seen_names:
                candidates.append(c)
                seen_names.add(c['standard_name'].upper())
        
        if not candidates:
            logger.debug(f"No candidates found for {protein_input} in wide search.")
        
        # 2. Priority 1: Exact Match
        protein_input_upper = protein_input.upper()
        for c in candidates:
            if c['standard_name'].upper() == protein_input_upper:
                logger.info(f"V1 - Exact match: {protein_input} -> {c['standard_name']}")
                c['confidence'] = 'high (exact)'
                self.cache.set(cache_key, c, ttl=86400*30)
                return c
        
        # 3. Priority 2: Alias Match
        for c in candidates:
            aliases_upper = [a.upper() for a in c.get('aliases', [])]
            if protein_input_upper in aliases_upper:
                logger.info(f"V2 - Alias match: {protein_input} -> {c['standard_name']}")
                c['confidence'] = 'medium (alias)'
                self.cache.set(cache_key, c, ttl=86400*30)
                return c
        
        # 4. Priority 3: LLM Assist
        if ai_service:
            logger.info(f"V1/V2 failed for {protein_input}. Attempting LLM-Assist.")
            suggestions = await self._get_llm_search_suggestions(protein_input, species_taxon, ai_service)
            
            if suggestions:
                logger.info(f"LLM suggested: {suggestions} for {protein_input}")
                for term in suggestions:
                    strict_result = await self._normalize_via_ncbi_gene_strict(term, species_taxon)
                    if not strict_result:
                        strict_result = await self._normalize_via_uniprot_strict(term, species_taxon)
                    
                    if strict_result:
                        logger.info(f"V3 - LLM-Assist success: {protein_input} -> {strict_result['standard_name']}")
                        strict_result['confidence'] = 'high (LLM-assisted)'
                        self.cache.set(cache_key, strict_result, ttl=86400*30)
                        return strict_result
            else:
                logger.info(f"LLM returned no suggestions for {protein_input}.")
        
        logger.warning(f"V4 - Strict Fail: Unable to normalize '{protein_input}'. It will be skipped.")
        self.cache.set(cache_key, None, ttl=86400*7) 
        return None

    async def _normalize_via_uniprot(self, protein_input: str, species_taxon: int) -> List[Dict]:
        """Fetch Top 5 candidates from UniProt."""
        candidates = []
        try:
            url = 'https://rest.uniprot.org/uniprotkb/search'
            params = {
                'query': f'({protein_input}) AND (organism_id:{species_taxon})',
                'format': 'json',
                'size': 5,
                'fields': 'gene_names,protein_name,accession,gene_synonym'
            }
            await self.rate_limiter.wait('uniprot')
            async with aiohttp.ClientSession() as session:
                data = await self._api_call(session, url, params=params)
                
                for entry in data.get('results', []):
                    standard_name = None
                    aliases = []
                    
                    if entry.get('genes') and len(entry['genes']) > 0:
                        gene_entry = entry['genes'][0]
                        standard_name = gene_entry.get('geneName', {}).get('value')
                        aliases.extend([syn.get('value') for syn in gene_entry.get('synonyms', []) if syn.get('value')])

                    if standard_name:
                        candidates.append({
                            'standard_name': standard_name,
                            'aliases': list(set(aliases)),
                            'accession': entry.get('primaryAccession'),
                            'source': 'uniprot'
                        })
        except Exception as e:
            logger.debug(f"UniProt candidate search failed: {e}")
        return candidates

    async def _normalize_via_ncbi_gene(self, protein_input: str, species_taxon: int) -> List[Dict]:
        """Fetch Top 5 candidates from NCBI Gene."""
        candidates = []
        try:
            url = f"{self.ncbi_base}/esearch.fcgi"
            params = {
                'db': 'gene',
                'term': f'(({protein_input}[Gene Name]) OR ({protein_input}[All Fields])) AND {species_taxon}[Taxonomy ID]',
                'retmode': 'json',
                'retmax': 5
            }
            await self.rate_limiter.wait('ncbi')
            async with aiohttp.ClientSession() as session:
                data = await self._api_call(session, url, params=params)
                id_list = data.get('esearchresult', {}).get('idlist', [])
                if not id_list:
                    return []

                fetch_url = f"{self.ncbi_base}/esummary.fcgi"
                fetch_params = {
                    'db': 'gene',
                    'id': ','.join(id_list),
                    'retmode': 'json'
                }
                await self.rate_limiter.wait('ncbi')
                summary_data = await self._api_call(session, fetch_url, params=fetch_params)
                
                results = summary_data.get('result', {})
                if 'uids' in results:
                    results.pop('uids')
                
                for gene_id in id_list:
                    if gene_id not in results: continue
                    result = results[gene_id]
                    
                    official_symbol = result.get('name', '')
                    aliases = result.get('otheraliases', '').split(', ')
                    
                    if official_symbol:
                        candidates.append({
                            'standard_name': official_symbol,
                            'aliases': [a.strip() for a in aliases if a.strip()],
                            'gene_id': gene_id,
                            'source': 'ncbi_gene'
                        })
        except Exception as e:
            logger.debug(f"NCBI Gene candidate search failed: {e}")
        return candidates

    async def _normalize_via_uniprot_strict(self, search_term: str, species_taxon: int) -> Optional[Dict]:
        """Fetch one exact match from UniProt."""
        try:
            url = 'https://rest.uniprot.org/uniprotkb/search'
            params = {
                'query': f'(gene_exact:"{search_term}") AND (organism_id:{species_taxon})',
                'format': 'json',
                'size': 1,
                'fields': 'gene_names,protein_name,accession,gene_synonym'
            }
            await self.rate_limiter.wait('uniprot')
            async with aiohttp.ClientSession() as session:
                data = await self._api_call(session, url, params=params)
                
                if not data.get('results'):
                    return None
                
                entry = data['results'][0]
                standard_name = None
                aliases = []
                
                if entry.get('genes') and len(entry['genes']) > 0:
                    gene_entry = entry['genes'][0]
                    standard_name = gene_entry.get('geneName', {}).get('value')
                    aliases.extend([syn.get('value') for syn in gene_entry.get('synonyms', []) if syn.get('value')])

                if standard_name and standard_name.upper() == search_term.upper():
                    return {
                        'standard_name': standard_name,
                        'aliases': list(set(aliases)),
                        'accession': entry.get('primaryAccession'),
                        'source': 'uniprot'
                    }
        except Exception as e:
            logger.debug(f"UniProt strict search failed: {e}")
        return None

    async def _normalize_via_ncbi_gene_strict(self, search_term: str, species_taxon: int) -> Optional[Dict]:
        """Fetch one exact match from NCBI Gene."""
        try:
            url = f"{self.ncbi_base}/esearch.fcgi"
            params = {
                'db': 'gene',
                'term': f'"{search_term}"[Gene Name] AND {species_taxon}[Taxonomy ID]',
                'retmode': 'json',
                'retmax': 1
            }
            await self.rate_limiter.wait('ncbi')
            async with aiohttp.ClientSession() as session:
                data = await self._api_call(session, url, params=params)
                id_list = data.get('esearchresult', {}).get('idlist', [])
                if not id_list:
                    return None
                
                gene_id = id_list[0]
                fetch_url = f"{self.ncbi_base}/esummary.fcgi"
                fetch_params = {'db': 'gene', 'id': gene_id, 'retmode': 'json'}
                
                await self.rate_limiter.wait('ncbi')
                summary_data = await self._api_call(session, fetch_url, params=fetch_params)
                
                result = summary_data.get('result', {}).get(gene_id, {})
                official_symbol = result.get('name', '')
                
                if official_symbol and official_symbol.upper() == search_term.upper():
                    aliases = result.get('otheraliases', '').split(', ')
                    return {
                        'standard_name': official_symbol,
                        'aliases': [a.strip() for a in aliases if a.strip()],
                        'gene_id': gene_id,
                        'source': 'ncbi_gene'
                    }
        except Exception as e:
            logger.debug(f"NCBI Gene strict search failed: {e}")
        return None

    async def _get_llm_search_suggestions(self, protein_input: str, species_taxon: int, ai_service: 'AIService') -> List[str]:
        """Get search term suggestions from LLM."""
        if not ai_service:
            return []
        
        try:
            system_prompt = "You are a bioinformatics expert. Your task is to provide the most likely official gene symbols for a database search."
            user_prompt = f"""I am searching for the protein "{protein_input}" in species taxon {species_taxon}. My database search failed to find an exact match.
Please provide a JSON list of the most likely official gene symbols or common aliases I should try searching for.

Requirements:
1. Return ONLY a JSON object in the format: {{"search_terms": ["Suggestion1", "Suggestion2"]}}
2. Prioritize official gene symbols.
3. If the input seems invalid or you are unsure, return: {{"search_terms": []}}

Example for "Pik3rI" (typo):
{{"search_terms": ["Pik3r1", "Pi3k-r1"]}}

Input: "{protein_input}"
Species: {species_taxon}
"""
            response = await ai_service.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                is_json=True,
                temperature=0.0
            )
            
            if response and isinstance(response, dict) and isinstance(response.get('search_terms'), list):
                return response['search_terms']
            else:
                logger.warning(f"LLM suggestion returned invalid format: {response}")
                return []
        except Exception as e:
            logger.error(f"LLM suggestion failed: {e}")
            return []

    async def get_human_ortholog(self, gene_symbol: str, species_taxon: int) -> Optional[str]:
        """Get human ortholog for non-human gene via UniProt API or Name Matching."""
        if species_taxon == 9606:
            return None
        
        cache_key = f"ortholog_uniprot_{species_taxon}_{gene_symbol}"
        if cached := self.cache.get(cache_key):
            return cached
        
        # Strategy 1: Simple Name Matching (High success rate for Mouse/Human)
        human_symbol_guess = gene_symbol.upper()
        if await self._verify_human_symbol_exists(human_symbol_guess):
            logger.info(f"Ortholog found via symbol match: {gene_symbol} -> {human_symbol_guess}")
            self.cache.set(cache_key, human_symbol_guess, ttl=86400*30)
            return human_symbol_guess

        # Strategy 2: UniProt (Existing logic)
        try:
            url = 'https://rest.uniprot.org/uniprotkb/search'
            params = {
                'query': f'(gene_exact:"{gene_symbol}") AND (organism_id:{species_taxon})',
                'format': 'json',
                'size': 1,
                'fields': 'accession,gene_names,organism_name,xref_ensembl'
            }
            await self.rate_limiter.wait('uniprot')
            
            async with aiohttp.ClientSession() as session:
                data = await self._api_call(session, url, params=params)
                
                if data.get('results'):
                    accession = data['results'][0]['primaryAccession']
                    ortholog_url = f'https://rest.uniprot.org/uniprotkb/{accession}'
                    ortholog_params = {'fields': 'cc_similarity', 'format': 'json'}
                    
                    await self.rate_limiter.wait('uniprot')
                    ortholog_data = await self._api_call(session, ortholog_url, params=ortholog_params)
                    
                    for comment in ortholog_data.get('comments', []):
                        if comment.get('commentType') == 'SIMILARITY':
                            for text_obj in comment.get('texts', []):
                                value = text_obj.get('value', '')
                                if 'human' in value.lower() and 'ortholog' in value.lower():
                                    match = re.search(r'gene\s*([A-Za-z0-9]+)', value, re.IGNORECASE)
                                    if match:
                                        human_symbol = match.group(1).upper()
                                        self.cache.set(cache_key, human_symbol, ttl=86400*30)
                                        return human_symbol
        except Exception as e:
            logger.error(f"UniProt ortholog query failed for {gene_symbol}: {e}")

        # Strategy 3: NCBI HomoloGene Fallback
        ncbi_ortholog = await self._get_ncbi_ortholog_fallback(gene_symbol, species_taxon)
        if ncbi_ortholog:
            self.cache.set(cache_key, ncbi_ortholog, ttl=86400*30)
            return ncbi_ortholog
        
        logger.info(f"No human ortholog found for {gene_symbol}")
        self.cache.set(cache_key, None, ttl=86400*7)
        return None

    async def _verify_human_symbol_exists(self, symbol: str) -> bool:
        """Helper: Check if a gene symbol exists in Human (Taxon 9606) via NCBI."""
        try:
            search_url = f"{self.ncbi_base}/esearch.fcgi"
            params = {
                'db': 'gene',
                'term': f'({symbol}[Gene Name]) AND 9606[Taxonomy ID]',
                'retmode': 'json',
                'retmax': 1
            }
            await self.rate_limiter.wait('ncbi')
            async with aiohttp.ClientSession() as session:
                data = await self._api_call(session, search_url, params=params)
                id_list = data.get('esearchresult', {}).get('idlist', [])
                return len(id_list) > 0
        except Exception:
            return False

    async def _get_ncbi_ortholog_fallback(self, gene_symbol: str, species_taxon: int) -> Optional[str]:
        """Fallback to NCBI to get human ortholog using the HomoloGene ID (more reliable than elink)."""
        try:
            search_url = f"{self.ncbi_base}/esearch.fcgi"
            params = {
                'db': 'gene',
                'term': f'({gene_symbol}[Gene Name]) AND {species_taxon}[Taxonomy ID]',
                'retmode': 'json',
                'retmax': 1
            }
            await self.rate_limiter.wait('ncbi')
            async with aiohttp.ClientSession() as session:
                data = await self._api_call(session, search_url, params=params)
                id_list = data.get('esearchresult', {}).get('idlist', [])
                if not id_list: return None
                gene_id = id_list[0]
                
                summary_url = f"{self.ncbi_base}/esummary.fcgi"
                summary_params = {'db': 'gene', 'id': gene_id, 'retmode': 'json'}
                await self.rate_limiter.wait('ncbi')
                summary = await self._api_call(session, summary_url, params=summary_params)
                
                homologene_group = summary.get('result', {}).get(gene_id, {}).get('homologene', -1)
                if homologene_group == -1 or not isinstance(homologene_group, dict): return None

                hid = homologene_group.get('hid')

                homologene_search_url = f"{self.ncbi_base}/esearch.fcgi"
                homologene_search_params = {
                    'db': 'homologene',
                    'term': f'{hid}[HomoloGene ID] AND 9606[Taxonomy ID]',
                    'retmode': 'json',
                    'retmax': 1
                }
                await self.rate_limiter.wait('ncbi')
                homologene_data = await self._api_call(session, homologene_search_url, params=homologene_search_params)
                
                human_gene_id_list = homologene_data.get('esearchresult', {}).get('idlist', [])
                if not human_gene_id_list: return None

                human_gene_id = human_gene_id_list[0]
                fetch_url = f"{self.ncbi_base}/esummary.fcgi"
                fetch_params = {'db': 'gene', 'id': human_gene_id, 'retmode': 'json'}
                await self.rate_limiter.wait('ncbi')
                human_summary = await self._api_call(session, fetch_url, params=fetch_params)

                human_symbol = human_summary.get('result', {}).get(human_gene_id, {}).get('name')
                
                if human_symbol:
                    logger.info(f"NCBI Fallback Ortholog found: {gene_symbol} -> {human_symbol}")
                    return human_symbol
                return None
                
        except Exception as e:
            logger.error(f"NCBI ortholog fallback failed for {gene_symbol}: {e}")
            return None


    async def get_mesh_terms(self, protein: str, species_taxon: int = 9606) -> List[str]:
        """Get MeSH terms for protein via NCBI Gene"""
        cache_key = f"mesh_{species_taxon}_{protein}"
        if cached := self.cache.get(cache_key):
            return cached

        try:
            search_url = f"{self.ncbi_base}/esearch.fcgi"
            params = {
                'db': 'gene',
                'term': f'({protein}[Gene Name]) AND {species_taxon}[Taxonomy ID]',
                'retmode': 'json',
                'retmax': 1
            }

            await self.rate_limiter.wait('ncbi')

            async with aiohttp.ClientSession() as session:
                data = await self._api_call(session, search_url, params=params)

                id_list = data.get('esearchresult', {}).get('idlist', [])
                if not id_list:
                    self.cache.set(cache_key, [], ttl=86400*7)
                    return []

                gene_id = id_list[0]
                
                link_url = f"{self.ncbi_base}/elink.fcgi"
                link_params = {
                    'dbfrom': 'gene',
                    'db': 'mesh',
                    'id': gene_id,
                    'retmode': 'json'
                }

                await self.rate_limiter.wait('ncbi')
                link_data = await self._api_call(session, link_url, params=link_params)

                mesh_ids = []
                linksets = link_data.get('linksets', [])
                for linkset in linksets:
                    linksetdbs = linkset.get('linksetdbs', [])
                    for linksetdb in linksetdbs:
                        if linksetdb.get('linkname') == 'gene_mesh':
                            mesh_ids = linksetdb.get('links', [])
                            break

                if not mesh_ids:
                    self.cache.set(cache_key, [], ttl=86400*7)
                    return []

                fetch_url = f"{self.ncbi_base}/efetch.fcgi"
                fetch_params = {
                    'db': 'mesh',
                    'id': ','.join(mesh_ids[:10]),
                    'retmode': 'xml'
                }

                await self.rate_limiter.wait('ncbi')
                xml_text = await self._api_call(session, fetch_url, params=fetch_params)

                mesh_terms = []
                if xml_text:
                    root = ET.fromstring(xml_text)
                    for descriptor in root.findall('.//DescriptorRecord'):
                        name_elem = descriptor.find('.//DescriptorName/String')
                        if name_elem is not None and name_elem.text:
                            mesh_terms.append(name_elem.text)

                logger.info(f"Found {len(mesh_terms)} MeSH terms for {protein}")
                self.cache.set(cache_key, mesh_terms, ttl=86400*30)
                return mesh_terms

        except Exception as e:
            logger.warning(f"MeSH term lookup failed for {protein}: {e}")
            self.cache.set(cache_key, [], ttl=86400*7)
            return []

    async def get_citation_network(self, pmids: List[str], email: str, max_citations: int = 50) -> List[str]:
        """Get citation network via NCBI eLink"""
        if not pmids:
            return []

        cache_key = f"citations_{'_'.join(sorted(pmids[:10]))}"
        if cached := self.cache.get(cache_key):
            return cached[:max_citations]

        try:
            url = f"{self.ncbi_base}/elink.fcgi"
            params = {
                'dbfrom': 'pubmed',
                'db': 'pubmed',
                'linkname': 'pubmed_pubmed_citedin',
                'id': ','.join(pmids[:20]),
                'retmode': 'json',
                'email': email
            }

            await self.rate_limiter.wait('ncbi')

            async with aiohttp.ClientSession() as session:
                data = await self._api_call(session, url, params=params)

                citing_pmids = set()
                linksets = data.get('linksets', [])
                for linkset in linksets:
                    linksetdbs = linkset.get('linksetdbs', [])
                    for linksetdb in linksetdbs:
                        if linksetdb.get('linkname') == 'pubmed_pubmed_citedin':
                            citing_pmids.update(linksetdb.get('links', []))

                result = list(citing_pmids)[:max_citations]
                self.cache.set(cache_key, result, ttl=86400*7)
                logger.info(f"Found {len(result)} citing articles for {len(pmids)} PMIDs")
                return result

        except Exception as e:
            logger.error(f"Citation network fetch failed: {e}")
            return []

    async def query_comprehensive_data(
        self, 
        gene_symbol: str, 
        species_taxon: int = 9606, 
        species_kegg: str = 'hsa',
        human_ortholog_override: Optional[str] = None
    ) -> Optional[Dict]:
        """
        Comprehensive database query integrating multiple sources
        
        Args:
            gene_symbol: Gene symbol/protein name
            species_taxon: NCBI Taxonomy ID (default 9606 = Homo sapiens)
            species_kegg: KEGG species code (default 'hsa' = Homo sapiens)
            human_ortholog_override: Human ortholog gene name for expression queries
        
        Returns:
            Dictionary containing integrated database information or None if all queries fail
        """
        cache_key = f"comprehensive_{species_taxon}_{species_kegg}_{gene_symbol}_{human_ortholog_override or 'none'}"
        if cached := self.cache.get(cache_key):
            logger.info(f"Using cached comprehensive data for {gene_symbol}")
            return cached
        
        self._report_progress(f"Querying databases for: {gene_symbol}")
        logger.info(f"Starting comprehensive query for {gene_symbol}")
        
        result = {
            'gene_symbol': gene_symbol,
            'query_timestamp': datetime.now().isoformat(),
            'data_availability': {
                'uniprot': False,
                'kegg': False,
                'hpa': False,
                'gtex': False
            },
            'raw_data': {},
            'functional_data': {},
            'summary': {}
        }
        
        sources_found = 0
        
        core_tasks = [
            self.get_uniprot_functional_info(gene_symbol, species_taxon),
            self._query_kegg(gene_symbol, species_kegg)
        ]
        
        expression_query_gene = None
        if species_taxon == 9606:
            expression_query_gene = gene_symbol
        elif human_ortholog_override:
            expression_query_gene = human_ortholog_override
            logger.info(f"Using human ortholog override {expression_query_gene} for expression query.")
        
        if expression_query_gene:
            query_gene_upper = expression_query_gene.upper()
            core_tasks.append(self.get_hpa_expression(query_gene_upper))
            core_tasks.append(self.get_gtex_expression(query_gene_upper, 'Liver'))
        
        results = await asyncio.gather(*core_tasks, return_exceptions=True)
        uniprot_info = results[0] if len(results) > 0 else None
        kegg_info = results[1] if len(results) > 1 else None
        hpa_info = results[2] if len(results) > 2 else None
        gtex_info = results[3] if len(results) > 3 else None
        
        if isinstance(uniprot_info, Exception):
            logger.warning(f"UniProt query failed for {gene_symbol}: {uniprot_info}")
        elif uniprot_info:
            result['raw_data']['uniprot'] = uniprot_info
            result['data_availability']['uniprot'] = True
            sources_found += 1
            logger.info(f"UniProt data found for {gene_symbol}")
            
            result['functional_data']['uniprot_functions'] = {
                'accession': uniprot_info.get('accession', ''),
                'protein_name': uniprot_info.get('protein_name', ''),
                'functions': uniprot_info.get('function', []),
                'subcellular_location': uniprot_info.get('subcellular_location', []),
                'domains': uniprot_info.get('domains', []),
                'catalytic_activity': uniprot_info.get('catalytic_activity', []),
                'cofactors': uniprot_info.get('cofactors', []),
                'pathways': uniprot_info.get('pathways', []),
                'keywords': uniprot_info.get('keywords', [])
            }
        
        if isinstance(kegg_info, Exception):
            logger.warning(f"KEGG query failed for {gene_symbol}: {kegg_info}")
        elif kegg_info:
            result['raw_data']['kegg'] = kegg_info
            result['data_availability']['kegg'] = True
            sources_found += 1
            logger.info(f"KEGG data found for {gene_symbol}")
            
            result['functional_data']['kegg_pathways'] = {
                'gene_id': kegg_info.get('gene_id', ''),
                'pathways': kegg_info.get('details', {}).get('pathways', []),
                'pathway_count': len(kegg_info.get('details', {}).get('pathways', [])),
                'categorized_pathways': self._categorize_kegg_pathways(
                    kegg_info.get('details', {}).get('pathways', [])
                )
            }
        
        if isinstance(hpa_info, Exception):
            logger.warning(f"HPA query failed for {gene_symbol}: {hpa_info}")
        elif hpa_info:
            result['raw_data']['hpa'] = hpa_info
            result['data_availability']['hpa'] = True
            sources_found += 1
            logger.info(f"HPA data found for {gene_symbol}")
            
            result['functional_data']['hpa_expression'] = {
                'rna_tissue_expression': hpa_info.get('rna_tissue_expression', {}),
                'tissue_count': len(hpa_info.get('rna_tissue_expression', {})),
                'data_source': hpa_info.get('data_source', 'Human Protein Atlas')
            }
        
        if isinstance(gtex_info, Exception):
            logger.warning(f"GTEx query failed for {gene_symbol}: {gtex_info}")
        elif gtex_info:
            result['raw_data']['gtex'] = gtex_info
            result['data_availability']['gtex'] = True
            sources_found += 1
            logger.info(f"GTEx data found for {gene_symbol}")
            
            result['functional_data']['gtex_expression'] = {
                'tissue': gtex_info.get('tissue', ''),
                'median_tpm': gtex_info.get('median_tpm', 0),
                'unit': gtex_info.get('unit', 'TPM'),
                'sample_size': gtex_info.get('sample_size', 0),
                'data_source': gtex_info.get('data_source', 'GTEx Portal v8')
            }
        
        result['summary'] = {
            'sources_queried': len(core_tasks),
            'sources_found': sources_found,
            'data_quality': 'excellent' if sources_found >= 3 else 'good' if sources_found == 2 else 'limited' if sources_found == 1 else 'poor',
            'has_functional_info': result['data_availability']['uniprot'] or result['data_availability']['kegg'],
            'has_pathway_info': result['data_availability']['kegg'],
            'has_expression_info': result['data_availability']['hpa'] or result['data_availability']['gtex']
        }
        
        if sources_found > 0:
            self.cache.set(cache_key, result, ttl=86400*7)
            logger.info(f"Comprehensive query complete for {gene_symbol}: {sources_found}/{len(core_tasks)} sources successful")
            self._report_progress(f"Found annotations in {sources_found} database(s) for {gene_symbol}")
            return result
        else:
            logger.warning(f"No data found for {gene_symbol} in any database")
            self._report_progress(f"No annotations found for {gene_symbol}")
            return None
    
    # Added method to support query_comprehensive_data call which was missing in previous code snippet but required
    async def get_uniprot_functional_info(self, gene_symbol: str, species_taxon: int) -> Optional[Dict]:
        """Query UniProt for functional information"""
        cache_key = f"uniprot_func_{species_taxon}_{gene_symbol}"
        if cached := self.cache.get(cache_key):
            return cached

        try:
            url = 'https://rest.uniprot.org/uniprotkb/search'
            params = {
                'query': f'(gene_exact:"{gene_symbol}") AND (organism_id:{species_taxon}) AND (reviewed:true)',
                'format': 'json',
                'size': 1,
                'fields': 'accession,protein_name,cc_function,cc_subcellular_location,cc_domain,cc_catalytic_activity,cc_cofactor,cc_pathway,keyword'
            }
            
            await self.rate_limiter.wait('uniprot')
            
            async with aiohttp.ClientSession() as session:
                data = await self._api_call(session, url, params=params)
                
                if not data.get('results'):
                    # Fallback to unreviewed if reviewed not found
                    params['query'] = f'(gene_exact:"{gene_symbol}") AND (organism_id:{species_taxon})'
                    await self.rate_limiter.wait('uniprot')
                    data = await self._api_call(session, url, params=params)
                    if not data.get('results'):
                         return None
                
                entry = data['results'][0]
                
                def extract_comments(comment_type):
                    return [c['texts'][0]['value'] for c in entry.get('comments', []) 
                            if c['commentType'] == comment_type and c.get('texts')]

                result = {
                    'accession': entry.get('primaryAccession'),
                    'protein_name': entry.get('proteinDescription', {}).get('recommendedName', {}).get('fullName', {}).get('value'),
                    'function': extract_comments('FUNCTION'),
                    'subcellular_location': extract_comments('SUBCELLULAR LOCATION'),
                    'domains': extract_comments('DOMAIN'),
                    'catalytic_activity': extract_comments('CATALYTIC ACTIVITY'),
                    'cofactors': extract_comments('COFACTOR'),
                    'pathways': extract_comments('PATHWAY'),
                    'keywords': [k['name'] for k in entry.get('keywords', [])]
                }
                
                self.cache.set(cache_key, result, ttl=86400*30)
                return result
                
        except Exception as e:
            logger.error(f"UniProt query failed for {gene_symbol}: {e}")
            return None

    async def _query_kegg(self, gene_symbol: str, species: str = 'hsa') -> Optional[Dict]:
        """Query KEGG database using two-step process"""
        cache_key = f"kegg_{species}_{gene_symbol}"
        if cached := self.cache.get(cache_key):
            return cached
        
        gene_id = await self._get_kegg_gene_id_enhanced(gene_symbol, species)
        if not gene_id:
            logger.warning(f"KEGG: Gene {gene_symbol} not found in {species}")
            return None
        
        try:
            url = f'https://rest.kegg.jp/get/{gene_id}'
            await self.rate_limiter.wait('kegg')
            
            async with aiohttp.ClientSession() as session:
                detail_text = await self._api_call(session, url)
                
                if not detail_text or not detail_text.strip():
                    logger.error(f"KEGG: Empty response for {gene_id}")
                    return None
                
                result = {
                    'gene_id': gene_id,
                    'gene_symbol': gene_symbol,
                    'organism': species,
                    'details': self._parse_kegg_entry(detail_text)
                }
                
                self.cache.set(cache_key, result, ttl=86400*30)
                logger.info(f"KEGG: {gene_symbol} -> {gene_id}, found {len(result['details'].get('pathways', []))} pathways")
                return result
        
        except Exception as e:
            logger.error(f"KEGG query failed for {gene_symbol}: {e}")
            raise

    async def _get_kegg_gene_id_enhanced(self, gene_symbol: str, organism: str) -> Optional[str]:
        """Enhanced KEGG gene ID lookup with smart matching"""
        url = f'https://rest.kegg.jp/find/{organism}/{gene_symbol}'
        await self.rate_limiter.wait('kegg')
        
        try:
            async with aiohttp.ClientSession() as session:
                text = await self._api_call(session, url)
                
                if not text or not text.strip():
                    return None
                
                lines = text.strip().split('\n')
                if not lines:
                    return None
                
                gene_symbol_lower = gene_symbol.lower()
                
                perfect_match_id = None
                first_match_id = lines[0].split('\t')[0].strip()

                for line in lines:
                    parts = line.split('\t')
                    if len(parts) < 2:
                        continue
                    
                    gene_id = parts[0].strip()
                    names_and_desc = parts[1]
                    
                    symbols_part_match = re.match(r'^([A-Za-z0-9_.;\s-]+)(,|$)', names_and_desc)
                    if not symbols_part_match:
                        continue
                    
                    symbols_string = symbols_part_match.group(1).strip()
                    
                    names_list = [name.strip().lower() for name in symbols_string.split(';')]
                    
                    if gene_symbol_lower in names_list:
                        if names_list[0] == gene_symbol_lower:
                            logger.info(f"KEGG: {gene_symbol} -> {gene_id} (exact primary match)")
                            return gene_id
                        
                        if perfect_match_id is None:
                            perfect_match_id = gene_id
                
                if perfect_match_id:
                    logger.info(f"KEGG: {gene_symbol} -> {perfect_match_id} (exact alias match)")
                    return perfect_match_id
                
                logger.warning(f"KEGG: No exact match for {gene_symbol} in results. Using first result: {first_match_id}")
                return first_match_id
                    
        except Exception as e:
            logger.error(f"KEGG gene ID lookup failed for {gene_symbol}: {e}")
            
        return None


    def _parse_kegg_entry(self, entry_text: str) -> Dict:
        """Parse KEGG gene entry format"""
        result = {
            'definition': '',
            'organism': '',
            'pathways': [],
            'orthologs': [],
            'disease_associations': [],
            'modules': []
        }
        
        if not entry_text:
            return result
        
        current_section = None
        lines = entry_text.split('\n')
        
        for line in lines:
            if not line.strip():
                continue
            
            if line[0] != ' ':
                current_section = line.split()[0]
            
            if current_section == 'DEFINITION':
                if line.startswith('DEFINITION'):
                    result['definition'] = line.replace('DEFINITION', '').strip()
                elif line.startswith(' '):
                    result['definition'] += ' ' + line.strip()
            
            elif current_section == 'ORGANISM':
                if line.startswith('ORGANISM'):
                    result['organism'] = line.replace('ORGANISM', '').strip()
            
            elif current_section == 'PATHWAY':
                if line.startswith(' ') and line.strip():
                    parts = line.strip().split(maxsplit=1)
                    if len(parts) == 2:
                        result['pathways'].append({
                            'pathway_id': parts[0],
                            'pathway_name': parts[1]
                        })
            
            elif current_section == 'DISEASE':
                if line.startswith(' ') and line.strip():
                    parts = line.strip().split(maxsplit=1)
                    if len(parts) == 2:
                        result['disease_associations'].append({
                            'disease_id': parts[0],
                            'disease_name': parts[1]
                        })
            
            elif current_section == 'MODULE':
                if line.startswith(' ') and line.strip():
                    parts = line.strip().split(maxsplit=1)
                    if len(parts) == 2:
                        result['modules'].append({
                            'module_id': parts[0],
                            'module_name': parts[1]
                        })

            elif current_section == 'ORTHOLOGY':
                if line.startswith(' ') and line.strip():
                    parts = line.strip().split(maxsplit=1)
                    if len(parts) == 2:
                        result['orthologs'].append({
                            'ortholog_id': parts[0],
                            'ortholog_name': parts[1]
                        })
        
        return result

    def _categorize_kegg_pathways(self, pathways: List[Dict]) -> Dict:
        """Categorize KEGG pathways by functional class"""
        categories = {
            'metabolism': [],
            'genetic_information': [],
            'environmental_information': [],
            'cellular_processes': [],
            'organismal_systems': [],
            'human_diseases': [],
            'other': []
        }
        
        for pathway in pathways:
            pathway_id = pathway.get('pathway_id', '')
            pathway_name = pathway.get('pathway_name', '').lower()
            
            if any(term in pathway_name for term in ['metabolism', 'biosynthesis', 'degradation', 'metabolic']):
                categories['metabolism'].append(pathway)
            elif any(term in pathway_name for term in ['replication', 'transcription', 'translation', 'dna', 'rna']):
                categories['genetic_information'].append(pathway)
            elif any(term in pathway_name for term in ['signal', 'signaling', 'transduction', 'receptor']):
                categories['environmental_information'].append(pathway)
            elif any(term in pathway_name for term in ['cell cycle', 'apoptosis', 'autophagy', 'transport', 'cellular']):
                categories['cellular_processes'].append(pathway)
            elif any(term in pathway_name for term in ['immune', 'nervous', 'endocrine', 'circulatory', 'digestive']):
                categories['organismal_systems'].append(pathway)
            elif any(term in pathway_name for term in ['cancer', 'disease', 'infection', 'disorder']):
                categories['human_diseases'].append(pathway)
            else:
                categories['other'].append(pathway)
        
        return {k: v for k, v in categories.items() if v}


class AIService:
    """AI Model Service"""

    MODEL_CONTEXT_LIMITS = {
        'gemini-1.5-flash': 1000000,
        'gemini-1.5-pro': 2000000,
        'gemini-2.5-flash-preview-09-2025': 1000000,
        'gemini-2.5-pro-preview-06-05': 2000000,
        'qwen-plus': 30000,
        'qwen-max': 64000,
        'deepseek-chat': 64000,
        'deepseek-reasoner': 64000
    }

    SAFE_INPUT_LIMITS = {
        'gemini-1.5-flash': 950000,
        'gemini-1.5-pro': 1950000,
        'gemini-2.5-flash-preview-09-2025': 950000,
        'gemini-2.5-pro-preview-06-05': 1950000,
        'qwen-plus': 20000,
        'qwen-max': 20000,
        'deepseek-chat': 50000,
        'deepseek-reasoner': 50000
    }

    def __init__(self, model_name: str, api_key: str, proxy: str = None):
        self.model_name = model_name
        self.api_key = api_key
        self.proxy = proxy
        self.rate_limiter = RateLimiter()
        logger.info(f"AI service initialized (model: {self.model_name})")

    def get_safe_token_limit(self) -> int:
        """Get safe token limit"""
        return self.SAFE_INPUT_LIMITS.get(self.model_name, 30000)

    async def _api_call(self, session: aiohttp.ClientSession, url: str, method: str = 'post', headers: Dict = None, json_data: Dict = None, timeout: int = 600, params: Dict = None) -> Dict:
        """Generic API call"""
        request_kwargs = {
            'headers': headers,
            'json': json_data,
            'timeout': aiohttp.ClientTimeout(total=timeout)
        }
        
        if params:
            request_kwargs['params'] = params

        if self.proxy:
            request_kwargs['proxy'] = self.proxy

        async with session.request(method, url, **request_kwargs) as response:
            response.raise_for_status()
            return await response.json()

    def _clean_json_response(self, text: str) -> str:
        """Clean JSON response"""
        text = re.sub(r'^```json\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'\s*```$', '', text, flags=re.MULTILINE)
        text = text.strip()
        return text

    def _attempt_json_repair(self, text: str) -> Optional[Dict]:
        """Attempt JSON repair"""
        try:
            brace_start = text.find('{')
            bracket_start = text.find('[')
            
            if brace_start == -1 and bracket_start == -1:
                return None
                
            if brace_start != -1 and (bracket_start == -1 or brace_start < bracket_start):
                start = brace_start
                end = text.rfind('}')
                if end > start:
                    return json.loads(text[start:end+1])
            elif bracket_start != -1:
                start = bracket_start
                end = text.rfind(']')
                if end > start:
                    return json.loads(text[start:end+1])
        except:
            pass
        return None

    def _validate_round1_structure(self, data: Dict) -> Dict:
        """Validate round1 structure"""
        if not isinstance(data, dict):
            logger.warning("Round1 response not a dict, attempting repair")
            raise ValueError("Response must be a dictionary")

        required_keys = {'biological_processes', 'molecular_functions', 'cellular_components', 'biochemical_activities'}
        missing = required_keys - set(data.keys())
        
        if missing:
            logger.warning(f"Round1 missing keys: {missing}. Adding empty arrays.")
            for key in missing:
                data[key] = []
        
        return data

    def _validate_round2_structure(self, data: Dict) -> Dict:
        """Validate round2 structure"""
        if not isinstance(data, dict):
            logger.warning("Round2 response not a dict, attempting repair")
            raise ValueError("Response must be a dictionary")

        required_keys = {'hypothesis', 'experimental_designs', 'biomarkers', 'clinical_applications', 'drug_targets'}
        missing = required_keys - set(data.keys())
        
        if missing:
            logger.warning(f"Round2 missing keys: {missing}. Adding defaults.")
            defaults = {
                'hypothesis': [],
                'experimental_designs': [],
                'biomarkers': [],
                'clinical_applications': [],
                'drug_targets': []
            }
            for key in missing:
                data[key] = defaults[key]
        
        return data

    async def generate(self, system_prompt: str, user_prompt: str, is_json: bool = False, temperature: float = 0.3) -> Any:
        """Generate AI response"""
        try:
            if self.model_name.startswith('gemini'):
                return await self._gemini_generate(system_prompt, user_prompt, is_json, temperature)
            elif self.model_name.startswith('qwen'):
                return await self._qwen_generate(system_prompt, user_prompt, is_json, temperature)
            elif self.model_name == 'deepseek-chat':
                return await self._deepseek_chat_generate(system_prompt, user_prompt, is_json, temperature)
            elif self.model_name == 'deepseek-reasoner':
                return await self._deepseek_reasoner_generate(system_prompt, user_prompt, is_json, temperature)
            else:
                raise ValueError(f"Unsupported model: {self.model_name}")
        except Exception as e:
            logger.error(f"AI generation failed: {e}")
            raise

    async def _gemini_generate(self, system_prompt: str, user_prompt: str, is_json: bool, temperature: float) -> Any:
        """Gemini generation"""
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent?key={self.api_key}'
        
        payload = {
            'contents': [{
                'parts': [
                    {'text': system_prompt},
                    {'text': user_prompt}
                ]
            }],
            'generationConfig': {
                'temperature': temperature,
                'maxOutputTokens': 8192
            }
        }
        
        if is_json:
            payload['generationConfig']['responseMimeType'] = 'application/json'
        
        await self.rate_limiter.wait('gemini')
        try:
            connector = aiohttp.TCPConnector(ssl=False) if self.proxy else None
            async with aiohttp.ClientSession(connector=connector) as session:
                data = await self._api_call(session, url, json_data=payload)
                candidates = data.get('candidates', [])
                if candidates and isinstance(candidates, list) and len(candidates) > 0:
                    parts = candidates[0].get('content', {}).get('parts', [])
                    if parts and isinstance(parts, list) and len(parts) > 0:
                        raw_text = parts[0].get('text', '')
                        
                        if is_json:
                            cleaned = self._clean_json_response(raw_text)
                            try:
                                return json.loads(cleaned)
                            except json.JSONDecodeError:
                                repaired = self._attempt_json_repair(cleaned)
                                if repaired:
                                    return repaired
                                raise
                        
                        return raw_text
                        
                logger.error("Gemini response structure unexpected or missing content.")
                raise KeyError("Could not extract content from Gemini response")
                
        except (KeyError, IndexError) as e:
            logger.error(f"Gemini response error: {e}. Response: {data if 'data' in locals() else 'N/A'}")
            raise

    async def _qwen_generate(self, system_prompt: str, user_prompt: str, is_json: bool, temperature: float) -> Any:
        """Qwen generation"""
        url = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
        payload = {
            'model': self.model_name,
            'messages': [
                {'role': 'system', 'content': system_prompt or 'You are a helpful assistant.'},
                {'role': 'user', 'content': user_prompt}
            ],
            'temperature': temperature,
            'max_tokens': 8192
        }
        if is_json:
            payload['response_format'] = {'type': 'json_object'}

        await self.rate_limiter.wait('qwen')
        try:
            connector = aiohttp.TCPConnector(ssl=False) if self.proxy else None
            async with aiohttp.ClientSession(connector=connector) as session:
                headers = {'Authorization': f'Bearer {self.api_key}'}
                data = await self._api_call(session, url, headers=headers, json_data=payload)
                choices = data.get('choices', [])
                if choices and isinstance(choices, list) and len(choices) > 0:
                    message = choices[0].get('message', {})
                    raw_text = message.get('content', '')
                    
                    if is_json:
                        cleaned = self._clean_json_response(raw_text)
                        try:
                            return json.loads(cleaned)
                        except json.JSONDecodeError:
                            repaired = self._attempt_json_repair(cleaned)
                            if repaired:
                                return repaired
                            raise
                    
                    return raw_text
                    
                logger.error("Qwen response structure unexpected or missing content.")
                raise KeyError("Could not extract content from Qwen response")

        except (KeyError, IndexError) as e:
            logger.error(f"Qwen response error: {e}. Response: {data if 'data' in locals() else 'N/A'}")
            raise

    async def _deepseek_chat_generate(self, system_prompt: str, user_prompt: str, is_json: bool, temperature: float) -> Any:
        """DeepSeek Chat"""
        url = 'https://api.deepseek.com/chat/completions'
        payload = {
            'model': 'deepseek-chat',
            'messages': [
                {'role': 'system', 'content': system_prompt or 'You are a helpful assistant.'},
                {'role': 'user', 'content': user_prompt}
            ],
            'temperature': temperature,
            'max_tokens': 8192
        }
        if is_json:
            payload['response_format'] = {'type': 'json_object'}

        await self.rate_limiter.wait('deepseek')
        try:
            connector = aiohttp.TCPConnector(ssl=False) if self.proxy else None
            async with aiohttp.ClientSession(connector=connector) as session:
                headers = {'Authorization': f'Bearer {self.api_key}'}
                data = await self._api_call(session, url, headers=headers, json_data=payload)
                choices = data.get('choices', [])
                if choices and isinstance(choices, list) and len(choices) > 0:
                    message = choices[0].get('message', {})
                    raw_text = message.get('content', '')
                    
                    if is_json:
                        cleaned = self._clean_json_response(raw_text)
                        try:
                            return json.loads(cleaned)
                        except json.JSONDecodeError:
                            repaired = self._attempt_json_repair(cleaned)
                            if repaired:
                                return repaired
                            raise
                    
                    return raw_text
                    
                logger.error("DeepSeek response structure unexpected or missing content.")
                raise KeyError("Could not extract content from DeepSeek response")

        except (KeyError, IndexError) as e:
            logger.error(f"DeepSeek response error: {e}. Response: {data if 'data' in locals() else 'N/A'}")
            raise

    async def _deepseek_reasoner_generate(self, system_prompt: str, user_prompt: str, is_json: bool, temperature: float) -> Any:
        """DeepSeek Reasoner"""
        url = 'https://api.deepseek.com/chat/completions'
        payload = {
            'model': 'deepseek-reasoner',
            'messages': [
                {'role': 'system', 'content': system_prompt or 'You are a helpful assistant.'},
                {'role': 'user', 'content': user_prompt}
            ],
            'temperature': temperature,
            'max_tokens': 8192
        }
        if is_json:
            payload['response_format'] = {'type': 'json_object'}

        await self.rate_limiter.wait('deepseek')
        try:
            connector = aiohttp.TCPConnector(ssl=False) if self.proxy else None
            async with aiohttp.ClientSession(connector=connector) as session:
                headers = {'Authorization': f'Bearer {self.api_key}'}
                data = await self._api_call(session, url, headers=headers, json_data=payload)
                choices = data.get('choices', [])
                if choices and isinstance(choices, list) and len(choices) > 0:
                    message = choices[0].get('message', {})
                    raw_text = message.get('content', '')
                    
                    if is_json:
                        cleaned = self._clean_json_response(raw_text)
                        try:
                            return json.loads(cleaned)
                        except json.JSONDecodeError:
                            repaired = self._attempt_json_repair(cleaned)
                            if repaired:
                                return repaired
                            raise
                    
                    return raw_text
                    
                logger.error("DeepSeek response structure unexpected or missing content.")
                raise KeyError("Could not extract content from DeepSeek response")

        except (KeyError, IndexError) as e:
            logger.error(f"DeepSeek response error: {e}. Response: {data if 'data' in locals() else 'N/A'}")
            raise

    # <<< STATIC METHOD FOR API TESTING >>>
    @staticmethod
    async def test_connection(model_name: str, api_key: str, proxy_url: Optional[str]) -> Tuple[bool, str]:
        """Static method for testing API connection without full instantiation."""
        
        test_system = "You are a test assistant."
        test_user = "Respond with 'OK' and the current UTC date."
        
        if model_name.startswith('gemini'):
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}'
            payload = {
                'contents': [{'parts': [{'text': test_system}, {'text': test_user}]}],
                'generationConfig': {'temperature': 0, 'maxOutputTokens': 50}
            }
            headers = {}
        
        elif model_name.startswith('qwen'):
            url = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
            payload = {
                'model': model_name,
                'messages': [
                    {'role': 'system', 'content': test_system},
                    {'role': 'user', 'content': test_user}
                ],
                'temperature': 0, 'max_tokens': 50
            }
            headers = {'Authorization': f'Bearer {api_key}'}
        
        elif model_name.startswith('deepseek'):
            url = 'https://api.deepseek.com/chat/completions'
            payload = {
                'model': model_name,
                'messages': [
                    {'role': 'system', 'content': test_system},
                    {'role': 'user', 'content': test_user}
                ],
                'temperature': 0, 'max_tokens': 50
            }
            headers = {'Authorization': f'Bearer {api_key}'}
        
        else:
            return False, f"Model '{model_name}' is not supported by the test harness."
        
        try:
            connector = aiohttp.TCPConnector(ssl=False) if proxy_url else None
            async with aiohttp.ClientSession(connector=connector) as session:
                request_kwargs = {
                    'headers': headers,
                    'json': payload,
                    'timeout': aiohttp.ClientTimeout(total=20)
                }
                if proxy_url:
                    request_kwargs['proxy'] = proxy_url

                async with session.post(url, **request_kwargs) as response:
                    response_text = await response.text()
                    if response.status == 200:
                        return True, f"Connection successful (HTTP 200). Response snippet: {response_text[:100]}..."
                    else:
                        return False, f"Connection failed (HTTP {response.status}). Error: {response_text[:200]}..."
        
        except aiohttp.ClientConnectorError as e:
            return False, f"Connection error: Could not connect to host. Check proxy/network. Details: {e}"
        except aiohttp.ClientResponseError as e:
            return False, f"HTTP error: {e.status}. Message: {e.message}"
        except asyncio.TimeoutError:
            return False, "Connection timed out after 20 seconds. Check proxy/network."
        except Exception as e:
            return False, f"An unexpected error occurred: {e}"