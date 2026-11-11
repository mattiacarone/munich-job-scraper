"""
Search titles for Mattia's role families (5 Nov 2026).

One list shared by the keyword-search sources (Arbeitsagentur, EURES, Himalayas, The Muse), so every source
looks for the same roles the application pipeline sends: implementation / integration / consulting,
solutions / forward-deployed engineering, technical customer success, project / product / operations.
Titles stay short so the boards' fuzzy search also returns close variants (e.g. "Consultant" -> "IT Consultant").
"""

FAMILY_TITLES = [
    # family 1: implementation, integration, consulting
    "Implementation Consultant",
    "Implementation Manager",
    "Implementation Specialist",
    "Integration Consultant",
    "Integration Engineer",
    "Integration Specialist",
    "Technical Consultant",
    "Technology Consultant",
    "Digital Consultant",
    "IT Consultant",
    "Salesforce Consultant",
    "ServiceNow Consultant",
    "MuleSoft",
    "Boomi",
    "CRM Consultant",
    "AI Consultant",
    "Business Analyst",
    "Business Systems Analyst",
    # family 2: solutions / forward-deployed / pre-sales engineering
    "Solutions Engineer",
    "Solution Consultant",
    "Solutions Architect",
    "Forward Deployed",
    "Sales Engineer",
    "Presales",
    "AI Solutions",
    # family 3: technical customer success
    "Customer Success Manager",
    "Customer Success Engineer",
    "Technical Account Manager",
    "Customer Onboarding",
    "Technical Support Engineer",
    "Partner Manager",
    # family 4: project / product / operations
    "Technical Project Manager",
    "IT Project Manager",
    "Technical Program Manager",
    "Product Manager",
    "Product Owner",
    "Delivery Manager",
    "Operations Manager",
]
