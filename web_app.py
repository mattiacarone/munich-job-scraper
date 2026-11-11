"""
Web Dashboard and Application Tracker for Mattia Carone's Munich Job Matcher.
Runs locally at http://127.0.0.1:8000 using Python standard library ThreadingHTTPServer.

Features:
- Live KPI metrics (Active Candidates, English Only, German is a Plus, Applied, Saved).
- Fast filtering (Language status, User application status, Min score, ATS source, Search keyword).
- 1-Click apply links direct to employer ATS.
- Application status management ('new', 'saved', 'applied', 'archived') with timestamp tracking.
- Live URL / ATS liveness check trigger and status badge.
- Job description viewer with matched skill highlights.
- Candidate notes editing.
"""

import html
import json
import logging
import os
import re
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import config
from database import Database
from liveness import LivenessEngine, check_single_job_liveness
import httpx

logger = logging.getLogger("WebApp")

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" class="h-full bg-slate-950 text-slate-100">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Munich Job Matcher &amp; Application Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
      tailwind.config = {
        darkMode: 'class',
        theme: {
          extend: {
            colors: {
              brand: {
                50: '#eef2ff',
                500: '#6366f1',
                600: '#4f46e5',
                700: '#4338ca',
              }
            }
          }
        }
      }
    </script>
    <style>
      /* Custom scrollbars */
      ::-webkit-scrollbar { width: 8px; height: 8px; }
      ::-webkit-scrollbar-track { background: #0f172a; }
      ::-webkit-scrollbar-thumb { background: #334155; border-radius: 4px; }
      ::-webkit-scrollbar-thumb:hover { background: #475569; }
      .badge-skill { background: rgba(99, 102, 241, 0.15); border: 1px solid rgba(99, 102, 241, 0.3); color: #c7d2fe; }
    </style>
</head>
<body class="h-full flex flex-col antialiased selection:bg-indigo-500 selection:text-white">

    <!-- Top Navigation Header -->
    <header class="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-30">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
            <div class="flex items-center space-x-3">
                <div class="w-10 h-10 rounded-xl bg-indigo-600 flex items-center justify-center font-bold text-white shadow-lg shadow-indigo-500/30">
                    MC
                </div>
                <div>
                    <h1 class="text-base font-bold text-white flex items-center gap-2">
                        Munich Job Discovery &amp; Tracker
                        <span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">
                            Mattia Carone
                        </span>
                    </h1>
                    <p class="text-xs text-slate-400">Pre-sales, Solutions Engineering &amp; iPaaS | Munich &amp; Remote DE</p>
                </div>
            </div>

            <div class="flex items-center space-x-3">
                <button id="btn-batch-verify" onclick="triggerBatchLiveness()" class="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 transition">
                    <span id="liveness-spinner" class="hidden animate-spin h-3.5 w-3.5 border-2 border-indigo-400 border-t-transparent rounded-full"></span>
                    <span id="liveness-icon">🔍</span>
                    <span>Verify Liveness</span>
                </button>
                <button onclick="fetchJobs()" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white shadow-sm transition">
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
                    <span>Refresh</span>
                </button>
            </div>
        </div>
    </header>

    <!-- Main Content Container -->
    <main class="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-6 space-y-6 overflow-y-auto">

        <!-- KPI Metrics Strip -->
        <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
            <div class="bg-slate-900 border border-slate-800 rounded-xl p-3.5 shadow-sm">
                <div class="text-xs font-medium text-slate-400">Active Matches</div>
                <div id="stat-total-candidates" class="text-2xl font-black text-indigo-400 mt-1">--</div>
                <div class="text-[11px] text-slate-500 mt-0.5">Passed hard filters</div>
            </div>
            <div class="bg-slate-900 border border-emerald-900/40 rounded-xl p-3.5 shadow-sm">
                <div class="text-xs font-medium text-emerald-400 flex items-center gap-1">
                    <span>🇬🇧 English Only</span>
                </div>
                <div id="stat-english-only" class="text-2xl font-black text-emerald-300 mt-1">--</div>
                <div class="text-[11px] text-emerald-500/80 mt-0.5">No German required</div>
            </div>
            <div class="bg-slate-900 border border-amber-900/40 rounded-xl p-3.5 shadow-sm">
                <div class="text-xs font-medium text-amber-400 flex items-center gap-1">
                    <span>🇩🇪 German is a Plus</span>
                </div>
                <div id="stat-german-plus" class="text-2xl font-black text-amber-300 mt-1">--</div>
                <div class="text-[11px] text-amber-500/80 mt-0.5">German optional/bonus</div>
            </div>
            <div class="bg-slate-900 border border-purple-900/40 rounded-xl p-3.5 shadow-sm">
                <div class="text-xs font-medium text-purple-400 flex items-center gap-1">
                    <span>🚀 Applied</span>
                </div>
                <div id="stat-applied" class="text-2xl font-black text-purple-300 mt-1">--</div>
                <div class="text-[11px] text-purple-500/80 mt-0.5">Applications sent</div>
            </div>
            <div class="bg-slate-900 border border-yellow-900/40 rounded-xl p-3.5 shadow-sm">
                <div class="text-xs font-medium text-yellow-400 flex items-center gap-1">
                    <span>⭐ Saved</span>
                </div>
                <div id="stat-saved" class="text-2xl font-black text-yellow-300 mt-1">--</div>
                <div class="text-[11px] text-yellow-500/80 mt-0.5">Bookmarked roles</div>
            </div>
            <div class="bg-slate-900 border border-slate-800 rounded-xl p-3.5 shadow-sm">
                <div class="text-xs font-medium text-slate-400">Total Scraped</div>
                <div id="stat-total-db" class="text-2xl font-black text-slate-300 mt-1">--</div>
                <div class="text-[11px] text-slate-500 mt-0.5" id="stat-closed-sub">-- closed</div>
            </div>
        </div>

        <!-- Filter & Search Toolbar -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-4">
            <div class="grid grid-cols-1 md:grid-cols-12 gap-3 items-center">
                
                <!-- Search Input -->
                <div class="md:col-span-4 relative">
                    <input type="text" id="search-input" placeholder="Search title, company, skills..."
                           class="w-full bg-slate-950 border border-slate-800 rounded-lg pl-9 pr-3 py-2 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-indigo-500 transition"
                           onkeydown="if(event.key==='Enter') fetchJobs()">
                    <svg class="w-4 h-4 text-slate-500 absolute left-3 top-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
                </div>

                <!-- Language Filter Pills -->
                <div class="md:col-span-5 flex items-center space-x-1.5 overflow-x-auto py-1">
                    <span class="text-xs font-semibold text-slate-400 mr-1">Language:</span>
                    <button class="lang-pill px-3 py-1 rounded-full text-xs font-semibold bg-indigo-600 text-white transition" data-lang="All" onclick="setLangFilter('All', this)">All</button>
                    <button class="lang-pill px-3 py-1 rounded-full text-xs font-semibold bg-slate-800 hover:bg-slate-700 text-slate-300 transition" data-lang="English Only" onclick="setLangFilter('English Only', this)">🇬🇧 English Only</button>
                    <button class="lang-pill px-3 py-1 rounded-full text-xs font-semibold bg-slate-800 hover:bg-slate-700 text-slate-300 transition" data-lang="German is a Plus" onclick="setLangFilter('German is a Plus', this)">🇩🇪 German is a Plus</button>
                </div>

                <!-- Status Filter -->
                <div class="md:col-span-3 flex items-center justify-end space-x-2">
                    <select id="status-select" onchange="fetchJobs()" class="bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs font-medium text-slate-300 focus:outline-none focus:border-indigo-500">
                        <option value="All">Status: All</option>
                        <option value="new" selected>Status: New / Unreviewed</option>
                        <option value="saved">Status: ⭐ Saved</option>
                        <option value="applied">Status: 🚀 Applied</option>
                        <option value="archived">Status: 📁 Archived</option>
                    </select>

                    <select id="sort-select" onchange="fetchJobs()" class="bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-xs font-medium text-slate-300 focus:outline-none focus:border-indigo-500">
                        <option value="score" selected>Sort: Highest Fit Score</option>
                        <option value="date">Sort: Most Recent Date</option>
                        <option value="company">Sort: Company Name</option>
                    </select>
                </div>
            </div>

            <!-- Additional filters bar -->
            <div class="flex flex-wrap items-center justify-between text-xs text-slate-400 pt-2 border-t border-slate-800/80 gap-3">
                <div class="flex items-center space-x-4">
                    <label class="flex items-center gap-1.5 cursor-pointer">
                        <span>Min Score:</span>
                        <select id="min-score-select" onchange="fetchJobs()" class="bg-slate-950 border border-slate-800 rounded px-2 py-1 text-xs text-slate-200">
                            <option value="0">Any Score (>0)</option>
                            <option value="30">30+ (Decent match)</option>
                            <option value="50" selected>50+ (High match)</option>
                            <option value="70">70+ (Elite tier)</option>
                        </select>
                    </label>

                    <label class="flex items-center gap-1.5 cursor-pointer">
                        <span>Source:</span>
                        <select id="source-select" onchange="fetchJobs()" class="bg-slate-950 border border-slate-800 rounded px-2 py-1 text-xs text-slate-200">
                            <option value="All">All Sources</option>
                            <option value="greenhouse">Greenhouse (Direct)</option>
                            <option value="ashby">Ashby (Direct)</option>
                            <option value="lever">Lever (Direct)</option>
                            <option value="personio">Personio (Direct)</option>
                            <option value="workable">Workable (Direct)</option>
                            <option value="smartrecruiters">SmartRecruiters (Direct)</option>
                            <option value="recruitee">Recruitee (Direct)</option>
                            <option value="linkedin">LinkedIn</option>
                            <option value="arbeitnow">Arbeitnow</option>
                            <option value="jobicy">Jobicy</option>
                        </select>
                    </label>

                    <label class="flex items-center gap-1.5 cursor-pointer">
                        <input type="checkbox" id="check-include-closed" onchange="fetchJobs()" class="rounded bg-slate-950 border-slate-700 text-indigo-600 focus:ring-0">
                        <span>Include Closed / Expired Postings</span>
                    </label>
                </div>

                <div id="results-count" class="font-medium text-slate-300">
                    Showing 0 postings
                </div>
            </div>
        </div>

        <!-- Postings Feed / List -->
        <div id="jobs-container" class="space-y-3.5">
            <!-- Dynamically populated -->
            <div class="text-center py-12 text-slate-500">
                <div class="inline-block animate-spin h-6 w-6 border-2 border-indigo-500 border-t-transparent rounded-full mb-2"></div>
                <div>Loading curated opportunities...</div>
            </div>
        </div>

        <!-- Pagination Controls -->
        <div id="pagination-bar" class="hidden flex items-center justify-between py-4 border-t border-slate-800 text-xs text-slate-400">
            <button id="btn-prev-page" onclick="prevPage()" class="px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 hover:bg-slate-800 disabled:opacity-40">Previous</button>
            <span id="page-indicator">Page 1</span>
            <button id="btn-next-page" onclick="nextPage()" class="px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 hover:bg-slate-800 disabled:opacity-40">Next</button>
        </div>
    </main>

    <!-- Slide-over / Modal for Job Description & Notes -->
    <div id="job-drawer" class="fixed inset-0 z-50 hidden bg-slate-950/80 backdrop-blur-sm flex justify-end">
        <div class="w-full max-w-2xl bg-slate-900 h-full border-l border-slate-800 p-6 flex flex-col shadow-2xl overflow-y-auto">
            <div class="flex items-center justify-between pb-4 border-b border-slate-800">
                <div>
                    <h2 id="drawer-title" class="text-lg font-bold text-white">Job Title</h2>
                    <p id="drawer-company" class="text-xs text-indigo-400 font-medium">Company Name</p>
                </div>
                <button onclick="closeDrawer()" class="p-1 rounded-lg hover:bg-slate-800 text-slate-400 hover:text-white transition">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                </button>
            </div>

            <!-- Drawer Meta -->
            <div class="py-4 space-y-3 border-b border-slate-800 text-xs">
                <div class="grid grid-cols-2 gap-2">
                    <div>
                        <span class="text-slate-500">Location:</span>
                        <span id="drawer-location" class="text-slate-200 ml-1 font-medium">Munich</span>
                    </div>
                    <div>
                        <span class="text-slate-500">Language:</span>
                        <span id="drawer-language" class="text-slate-200 ml-1 font-medium">English Only</span>
                    </div>
                    <div>
                        <span class="text-slate-500">Fit Score:</span>
                        <span id="drawer-score" class="text-indigo-400 ml-1 font-bold">85 / 100</span>
                    </div>
                    <div>
                        <span class="text-slate-500">Source:</span>
                        <span id="drawer-source" class="text-slate-200 ml-1 font-medium">Greenhouse</span>
                    </div>
                </div>

                <div>
                    <span class="text-slate-500 block mb-1">Matched Skills:</span>
                    <div id="drawer-skills" class="flex flex-wrap gap-1"></div>
                </div>

                <!-- Notes Section -->
                <div class="pt-2">
                    <label class="block text-slate-400 font-semibold mb-1">My Application Notes:</label>
                    <textarea id="drawer-notes-input" rows="2" placeholder="e.g. Sent bespoke CV, contacted recruiter on LinkedIn, awaiting response..."
                              class="w-full bg-slate-950 border border-slate-800 rounded-lg p-2.5 text-xs text-slate-200 focus:outline-none focus:border-indigo-500"></textarea>
                    <div class="flex justify-end mt-1.5">
                        <button onclick="saveCurrentJobNotes()" class="px-3 py-1 bg-indigo-600 hover:bg-indigo-500 text-white font-semibold rounded text-xs transition">Save Notes</button>
                    </div>
                </div>
            </div>

            <!-- Drawer Actions -->
            <div class="py-3 flex items-center justify-between border-b border-slate-800">
                <a id="drawer-apply-link" href="#" target="_blank" rel="noopener noreferrer"
                   class="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs rounded-lg shadow-sm transition inline-flex items-center gap-1.5">
                    <span>Apply Directly on ATS</span>
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path></svg>
                </a>

                <div class="flex items-center space-x-2">
                    <button id="drawer-btn-applied" onclick="drawerMarkStatus('applied')" class="px-3 py-1.5 bg-purple-600 hover:bg-purple-500 text-white rounded-lg text-xs font-semibold">Mark Applied 🚀</button>
                    <button id="drawer-btn-saved" onclick="drawerMarkStatus('saved')" class="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-yellow-300 rounded-lg text-xs font-semibold">Save ⭐</button>
                    <button id="drawer-btn-archive" onclick="drawerMarkStatus('archived')" class="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-400 rounded-lg text-xs font-semibold">Archive 📁</button>
                </div>
            </div>

            <!-- Drawer Description Body -->
            <div class="flex-1 py-4 text-xs text-slate-300 leading-relaxed space-y-4">
                <h3 class="font-bold text-slate-200 uppercase tracking-wider text-[11px]">Job Description</h3>
                <div id="drawer-description" class="prose prose-invert prose-xs max-w-none break-words"></div>
            </div>
        </div>
    </div>

    <!-- Frontend Application Logic -->
    <script>
      let currentLang = 'All';
      let currentOffset = 0;
      const limit = 50;
      let activeDrawerJob = null;

      function setLangFilter(lang, btn) {
        currentLang = lang;
        document.querySelectorAll('.lang-pill').forEach(p => {
          p.classList.remove('bg-indigo-600', 'text-white');
          p.classList.add('bg-slate-800', 'text-slate-300');
        });
        btn.classList.add('bg-indigo-600', 'text-white');
        btn.classList.remove('bg-slate-800', 'text-slate-300');
        currentOffset = 0;
        fetchJobs();
      }

      async function fetchStats() {
        try {
          const res = await fetch('/api/stats');
          const data = await res.json();
          document.getElementById('stat-total-candidates').innerText = data.total_candidates || 0;
          document.getElementById('stat-english-only').innerText = data.english_only || 0;
          document.getElementById('stat-german-plus').innerText = data.german_plus || 0;
          document.getElementById('stat-applied').innerText = data.applied || 0;
          document.getElementById('stat-saved').innerText = data.saved || 0;
          document.getElementById('stat-total-db').innerText = data.total_db || 0;
          document.getElementById('stat-closed-sub').innerText = (data.closed || 0) + ' verified closed';
        } catch (err) {
          console.error("Failed fetching stats", err);
        }
      }

      async function fetchJobs() {
        const query = document.getElementById('search-input').value.trim();
        const status = document.getElementById('status-select').value;
        const sort = document.getElementById('sort-select').value;
        const minScore = document.getElementById('min-score-select').value;
        const source = document.getElementById('source-select').value;
        const includeClosed = document.getElementById('check-include-closed').checked;

        const params = new URLSearchParams({
          q: query,
          lang: currentLang,
          status: status,
          sort: sort,
          min_score: minScore,
          source: source,
          closed: includeClosed ? 'true' : 'false',
          limit: limit,
          offset: currentOffset,
        });

        const container = document.getElementById('jobs-container');
        container.innerHTML = `
          <div class="text-center py-12 text-slate-500">
            <div class="inline-block animate-spin h-6 w-6 border-2 border-indigo-500 border-t-transparent rounded-full mb-2"></div>
            <div>Filtering postings...</div>
          </div>
        `;

        try {
          const res = await fetch('/api/jobs?' + params.toString());
          const data = await res.json();
          renderJobs(data.jobs, data.total);
          fetchStats(); // Keep KPIs in sync
        } catch (err) {
          container.innerHTML = `<div class="p-6 text-center text-red-400 bg-red-950/20 rounded-xl border border-red-900/40">Failed to load jobs: ${err.message}</div>`;
        }
      }

      function renderJobs(jobs, total) {
        const container = document.getElementById('jobs-container');
        document.getElementById('results-count').innerText = `Showing ${jobs.length} of ${total} matching positions`;

        if (!jobs || jobs.length === 0) {
          container.innerHTML = `
            <div class="bg-slate-900 border border-slate-800 rounded-xl p-12 text-center text-slate-400">
              <div class="text-3xl mb-2">🔍</div>
              <div class="text-base font-semibold text-slate-200">No positions found matching your criteria</div>
              <div class="text-xs text-slate-500 mt-1">Try switching language filters or broadening your search keywords.</div>
            </div>
          `;
          document.getElementById('pagination-bar').classList.add('hidden');
          return;
        }

        container.innerHTML = jobs.map(job => createJobCardHtml(job)).join('');

        // Pagination setup
        const paginationBar = document.getElementById('pagination-bar');
        paginationBar.classList.remove('hidden');
        document.getElementById('page-indicator').innerText = `Page ${Math.floor(currentOffset / limit) + 1} of ${Math.ceil(total / limit) || 1}`;
        document.getElementById('btn-prev-page').disabled = currentOffset <= 0;
        document.getElementById('btn-next-page').disabled = currentOffset + limit >= total;
      }

      function createJobCardHtml(job) {
        const isEnglishOnly = job.language_status === 'English Only';
        const langBadge = isEnglishOnly
          ? '<span class="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-semibold bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">🇬🇧 English Only</span>'
          : '<span class="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30">🇩🇪 German is a Plus</span>';

        const isClosed = Boolean(job.closed_at);
        const livenessBadge = isClosed
          ? '<span class="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-semibold bg-rose-500/15 text-rose-400 border border-rose-500/30">🔴 Closed</span>'
          : '<span class="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/20">🟢 Live</span>';

        let userStatusBadge = '';
        if (job.user_status === 'applied') {
          userStatusBadge = '<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-purple-500/20 text-purple-300 border border-purple-500/40">🚀 APPLIED</span>';
        } else if (job.user_status === 'saved') {
          userStatusBadge = '<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-yellow-500/20 text-yellow-300 border border-yellow-500/40">⭐ SAVED</span>';
        } else if (job.user_status === 'archived') {
          userStatusBadge = '<span class="px-2 py-0.5 rounded text-[11px] font-medium bg-slate-800 text-slate-400">📁 ARCHIVED</span>';
        }

        const scoreColor = job.score >= 70 ? 'text-emerald-400 border-emerald-500/40 bg-emerald-500/10'
                         : job.score >= 50 ? 'text-indigo-400 border-indigo-500/40 bg-indigo-500/10'
                         : 'text-slate-300 border-slate-700 bg-slate-800';

        const skillsHtml = (job.matched_skills || []).slice(0, 6).map(s => 
          `<span class="badge-skill px-2 py-0.5 rounded text-[10px] font-medium">${s}</span>`
        ).join(' ');

        const targetUrl = job.apply_url || job.url;
        const companyName = job.company_display || job.company || 'Unknown Company';

        return `
          <div class="bg-slate-900 border ${isClosed ? 'border-rose-950/60 opacity-70' : 'border-slate-800'} hover:border-slate-700 rounded-xl p-4 transition shadow-sm space-y-3">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
              <div class="space-y-1">
                <div class="flex flex-wrap items-center gap-2">
                  <h3 class="text-sm sm:text-base font-bold text-white hover:text-indigo-300 cursor-pointer" onclick='openJobDrawer(${JSON.stringify(job).replace(/'/g, "&#39;")})'>
                    ${escapeHtml(job.title)}
                  </h3>
                  ${langBadge}
                  ${livenessBadge}
                  ${userStatusBadge}
                </div>
                <div class="flex flex-wrap items-center gap-3 text-xs text-slate-400">
                  <span class="font-semibold text-slate-300">${escapeHtml(companyName)}</span>
                  <span>•</span>
                  <span>📍 ${escapeHtml(job.location || 'Munich, Germany')}</span>
                  <span>•</span>
                  <span class="capitalize text-slate-400">Source: ${job.source}</span>
                  ${job.posted_at ? `<span>•</span><span>📅 ${job.posted_at.split('T')[0]}</span>` : ''}
                </div>
              </div>

              <!-- Score Indicator -->
              <div class="flex items-center sm:flex-col items-end gap-1">
                <div class="px-2.5 py-1 rounded-lg border ${scoreColor} text-xs font-black">
                  ${Math.round(job.score)} pts
                </div>
                <span class="text-[10px] text-slate-500 font-mono">Profile Fit</span>
              </div>
            </div>

            <!-- Skills & Fit highlights -->
            <div class="flex flex-wrap items-center gap-1.5 pt-1">
              ${skillsHtml}
              ${job.fit_summary ? `<span class="text-[11px] text-slate-400 italic ml-2">(${escapeHtml(job.fit_summary)})</span>` : ''}
            </div>

            ${job.notes ? `<div class="bg-slate-950/70 border border-slate-800 rounded px-2.5 py-1.5 text-xs text-indigo-300 flex items-center gap-1.5">
              <span>📝</span><span>${escapeHtml(job.notes)}</span>
            </div>` : ''}

            <!-- Action Bar -->
            <div class="flex flex-wrap items-center justify-between pt-2 border-t border-slate-800/80 gap-2">
              <div class="flex items-center space-x-2">
                <a href="${targetUrl}" target="_blank" rel="noopener noreferrer"
                   class="px-3.5 py-1.5 rounded-lg text-xs font-bold bg-emerald-600 hover:bg-emerald-500 text-white transition inline-flex items-center gap-1.5 shadow-sm">
                  <span>Apply Now</span>
                  <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path></svg>
                </a>

                <button onclick='openJobDrawer(${JSON.stringify(job).replace(/'/g, "&#39;")})'
                        class="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 hover:bg-slate-700 text-slate-300 transition">
                  Details &amp; Description
                </button>
              </div>

              <!-- Status Action Buttons -->
              <div class="flex items-center space-x-1.5">
                <button onclick="updateJobStatus('${job.id}', 'applied')" title="Mark Applied"
                        class="px-2.5 py-1 rounded text-xs font-semibold ${job.user_status==='applied' ? 'bg-purple-600 text-white' : 'bg-slate-800 hover:bg-slate-700 text-purple-300'} transition">
                  🚀 Applied
                </button>
                <button onclick="updateJobStatus('${job.id}', 'saved')" title="Save"
                        class="px-2.5 py-1 rounded text-xs font-semibold ${job.user_status==='saved' ? 'bg-yellow-600 text-white' : 'bg-slate-800 hover:bg-slate-700 text-yellow-300'} transition">
                  ⭐ Save
                </button>
                <button onclick="updateJobStatus('${job.id}', 'archived')" title="Archive"
                        class="px-2 py-1 rounded text-xs font-medium bg-slate-800 hover:bg-slate-700 text-slate-400 transition">
                  📁
                </button>
                <button onclick="checkJobLiveness('${job.id}', this)" title="Re-check Liveness"
                        class="px-2 py-1 rounded text-xs font-medium bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-indigo-400 transition">
                  🔄
                </button>
              </div>
            </div>
          </div>
        `;
      }

      function escapeHtml(str) {
        if (!str) return '';
        return String(str)
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;');
      }

      async function updateJobStatus(jobId, status, notes=null) {
        try {
          const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/status`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: status, notes: notes }),
          });
          const result = await res.json();
          if (result.ok) {
            fetchJobs();
          }
        } catch (err) {
          alert('Error updating status: ' + err.message);
        }
      }

      async function checkJobLiveness(jobId, btn) {
        const originalText = btn.innerHTML;
        btn.innerHTML = '⏳';
        btn.disabled = true;
        try {
          const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/liveness`, { method: 'POST' });
          const result = await res.json();
          alert(`Liveness status: ${result.is_live ? '🟢 ACTIVE' : '🔴 CLOSED'}\nReason: ${result.reason}`);
          fetchJobs();
        } catch (err) {
          alert('Error checking liveness: ' + err.message);
        } finally {
          btn.innerHTML = originalText;
          btn.disabled = false;
        }
      }

      async function triggerBatchLiveness() {
        const btn = document.getElementById('btn-batch-verify');
        const spinner = document.getElementById('liveness-spinner');
        const icon = document.getElementById('liveness-icon');
        btn.disabled = true;
        spinner.classList.remove('hidden');
        icon.classList.add('hidden');

        try {
          const res = await fetch('/api/liveness/verify', { method: 'POST' });
          const data = await res.json();
          alert(`Batch Verification Complete!\n\nTotal checked: ${data.total}\nLive: ${data.live}\nNewly Marked Closed: ${data.closed}`);
          fetchJobs();
        } catch (err) {
          alert('Failed batch verification: ' + err.message);
        } finally {
          btn.disabled = false;
          spinner.classList.add('hidden');
          icon.classList.remove('hidden');
        }
      }

      function openJobDrawer(job) {
        activeDrawerJob = job;
        document.getElementById('drawer-title').innerText = job.title;
        document.getElementById('drawer-company').innerText = job.company_display || job.company;
        document.getElementById('drawer-location').innerText = job.location || 'Munich, Germany';
        document.getElementById('drawer-language').innerText = job.language_status;
        document.getElementById('drawer-score').innerText = `${Math.round(job.score)} / 100`;
        document.getElementById('drawer-source').innerText = job.source;
        document.getElementById('drawer-apply-link').href = job.apply_url || job.url;
        document.getElementById('drawer-notes-input').value = job.notes || '';

        const skillsContainer = document.getElementById('drawer-skills');
        skillsContainer.innerHTML = (job.matched_skills || []).map(s => 
          `<span class="badge-skill px-2 py-0.5 rounded text-[11px] font-medium">${s}</span>`
        ).join(' ') || '<span class="text-slate-500">None matched explicitly</span>';

        const descElem = document.getElementById('drawer-description');
        if (job.description) {
          descElem.innerHTML = job.description;
        } else {
          descElem.innerHTML = '<p class="text-slate-500 italic">No stored description available.</p>';
        }

        document.getElementById('job-drawer').classList.remove('hidden');
      }

      function closeDrawer() {
        document.getElementById('job-drawer').classList.add('hidden');
        activeDrawerJob = null;
      }

      async function saveCurrentJobNotes() {
        if (!activeDrawerJob) return;
        const notes = document.getElementById('drawer-notes-input').value.trim();
        await updateJobStatus(activeDrawerJob.id, activeDrawerJob.user_status || 'new', notes);
        alert('Notes saved successfully.');
      }

      async function drawerMarkStatus(status) {
        if (!activeDrawerJob) return;
        const notes = document.getElementById('drawer-notes-input').value.trim();
        await updateJobStatus(activeDrawerJob.id, status, notes);
        closeDrawer();
      }

      function prevPage() {
        if (currentOffset >= limit) {
          currentOffset -= limit;
          fetchJobs();
        }
      }

      function nextPage() {
        currentOffset += limit;
        fetchJobs();
      }

      // Initial load
      fetchStats();
      fetchJobs();
    </script>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    db: Database = None

    def _send_json(self, status_code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status_code: int, html_text: str):
        body = html_text.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query_params = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._send_html(HTTPStatus.OK, HTML_TEMPLATE)
            return

        if path == "/api/stats":
            stats = self.db.get_dashboard_stats()
            self._send_json(HTTPStatus.OK, stats)
            return

        if path == "/api/jobs":
            q = query_params.get("q", [None])[0]
            lang = query_params.get("lang", [None])[0]
            status = query_params.get("status", [None])[0]
            sort_by = query_params.get("sort", ["score"])[0]
            min_score = float(query_params.get("min_score", [0])[0] or 0)
            source = query_params.get("source", [None])[0]
            closed = query_params.get("closed", ["false"])[0].lower() == "true"
            limit = int(query_params.get("limit", [50])[0])
            offset = int(query_params.get("offset", [0])[0])

            jobs, total = self.db.get_web_jobs(
                query=q,
                language=lang,
                user_status=status,
                min_score=min_score,
                source=source,
                include_closed=closed,
                sort_by=sort_by,
                limit=limit,
                offset=offset,
            )

            self._send_json(HTTPStatus.OK, {
                "jobs": jobs,
                "total": total,
                "offset": offset,
                "limit": limit,
            })
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        # 1. Update job user status: /api/jobs/<id>/status
        m_status = re.match(r"^/api/jobs/(.+)/status$", path)
        if m_status:
            job_id = urllib.parse.unquote(m_status.group(1))
            status = payload.get("status", "new")
            notes = payload.get("notes")
            try:
                ok = self.db.set_job_user_status(job_id, status, notes)
                self._send_json(HTTPStatus.OK, {"ok": ok, "job_id": job_id, "status": status})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        # 2. Check single job liveness: /api/jobs/<id>/liveness
        m_liveness = re.match(r"^/api/jobs/(.+)/liveness$", path)
        if m_liveness:
            job_id = urllib.parse.unquote(m_liveness.group(1))
            # Fetch job details from DB
            with self.db._get_connection() as conn:
                row = conn.execute("SELECT id, title, company, source, url, apply_url, ats, ats_slug FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Job not found"})
                return

            job_dict = dict(row)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            }
            with httpx.Client(headers=headers, timeout=10.0, follow_redirects=True) as client:
                jid, is_live, reason = check_single_job_liveness(client, job_dict)

            if not is_live:
                now_iso = datetime.now(timezone.utc).isoformat()
                with self.db._get_connection() as conn:
                    conn.execute("UPDATE jobs SET closed_at = ? WHERE id = ?", (now_iso, jid))

            self._send_json(HTTPStatus.OK, {"ok": True, "job_id": jid, "is_live": is_live, "reason": reason})
            return

        # 3. Batch liveness verification: /api/liveness/verify
        if path == "/api/liveness/verify":
            engine = LivenessEngine(self.db, concurrency=15)
            # Check up to 100 candidate jobs synchronously
            results = engine.check_candidates(limit=100)
            self._send_json(HTTPStatus.OK, results)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")


def run_server(port: int = 8000, host: str = "127.0.0.1", db: Optional[Database] = None):
    """Start local web dashboard server."""
    if db is None:
        db = Database(config.db_path)

    DashboardRequestHandler.db = db
    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, DashboardRequestHandler)
    print(f"\n=======================================================")
    print(f"  🚀 Munich Job Matcher Web Dashboard Running!")
    print(f"  👉 Open URL: http://{host}:{port}")
    print(f"  Press Ctrl+C to stop the dashboard server.")
    print(f"=======================================================\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web dashboard server...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    run_server(port=port)
