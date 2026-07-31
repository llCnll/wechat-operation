from pathlib import Path


def test_authenticated_operations_pages_share_one_theme_control_and_initializer():
    topbar = Path("posts/templates/posts/includes/topbar.html").read_text(encoding="utf-8")
    operations_base = Path("posts/templates/posts/operations_base.html").read_text(encoding="utf-8")
    admin_base = Path("templates/admin/base_site.html").read_text(encoding="utf-8")
    initializer = Path("posts/templates/posts/includes/operations_theme_script.html").read_text(encoding="utf-8")

    assert 'data-operations-theme-toggle' in topbar
    assert "operations_theme.css" in operations_base
    assert "operations_theme_script.html" in operations_base
    assert "operations_theme.css" in admin_base
    assert "operations_theme_script.html" in admin_base
    assert 'const storageKey = "operations-theme"' in initializer
    assert "dataset.operationsTheme" in initializer
    assert "fetch(" not in initializer
    assert "}, true);" in initializer
    assert operations_base.index("{% block extra_head %}") < operations_base.index("operations_theme.css")


def test_admin_console_does_not_override_the_shared_topbar_layout():
    shared_stylesheet = Path("posts/static/posts/operations_theme.css").read_text(encoding="utf-8")
    console_stylesheet = Path("posts/static/posts/admin_console.css").read_text(encoding="utf-8")

    assert "#header.topbar,#header.topbar.admin-topbar { height:68px; min-height:68px; padding:0 max(24px,calc((100vw - 1320px)/2)); display:flex; flex-direction:row; flex-wrap:nowrap;" in shared_stylesheet
    assert "@media (max-width:1080px) { #header.topbar nav { display:none; } }" in shared_stylesheet
    assert "#header.topbar.admin-topbar" in shared_stylesheet
    assert "#header.topbar nav a:link,#header.topbar nav a:visited" in shared_stylesheet
    assert "#header.admin-topbar nav { display:flex;" not in console_stylesheet


def test_shared_theme_styles_cover_workspace_and_analysis_surfaces():
    stylesheet = Path("posts/static/posts/operations_theme.css").read_text(encoding="utf-8")

    assert 'body[data-operations-theme="dark"]' in stylesheet
    assert ".workspace" in stylesheet
    assert ".review-record" in stylesheet
    assert ".metric-panel" in stylesheet
    assert ".operations-theme-toggle" in stylesheet
    assert "#header.topbar.admin-topbar" in stylesheet


def test_shared_theme_styles_define_real_workspace_hierarchy_in_both_modes():
    stylesheet = Path("posts/static/posts/operations_theme.css").read_text(encoding="utf-8")

    for selector in (
        ".masthead,.workspace-header",
        ".workspace-filter,.period-toolbar",
        ".workspace,.mute-management-panel,.mute-history-panel",
        ".review-record-list",
        ".metric-p0-card,.metric-panel,.action-strip",
    ):
        assert selector in stylesheet
    assert 'body[data-operations-theme="dark"] :is(.masthead,.workspace-header)' in stylesheet
    assert 'body[data-operations-theme="dark"] :is(.workspace-filter,.period-toolbar)' in stylesheet


def test_operations_studio_keeps_distinct_queue_audit_and_analysis_work_modes():
    stylesheet = Path("posts/static/posts/operations_theme.css").read_text(encoding="utf-8")

    for selector in (
        "/* Content moderation: a continuous queue-to-decision canvas. */",
        "/* Mute management: retain scanning efficiency while separating maintenance from intent. */",
        "/* AI review: an audit ledger with a clear evidence and confirmation cadence. */",
        "/* Analytics: lead with time, then move from signal to explanation. */",
        "@media (prefers-reduced-motion:reduce)",
    ):
        assert selector in stylesheet


def test_content_management_subpages_share_a_stable_desktop_hero_height():
    stylesheet = Path("posts/static/posts/operations_theme.css").read_text(encoding="utf-8")

    assert ".masthead.compact-head { min-height:188px; height:188px; }" in stylesheet
    assert ".masthead,.masthead.compact-head,.workspace-header,.metric-workspace-head" in stylesheet


def test_analytics_reuses_the_shared_workspace_heading_structure():
    template = Path("reporting/templates/reporting/live_operations_analysis.html").read_text(encoding="utf-8")

    assert '<section class="masthead compact-head report-head metric-workspace-head">' in template
    assert '<p class="eyebrow">LIVE OPERATIONS ANALYTICS</p><h1>运营指标工作台</h1>' in template
    assert '<div class="head-context"><div class="metric-freshness">' in template


def test_content_decision_areas_use_distinct_semantic_color_surfaces():
    stylesheet = Path("posts/static/posts/operations_theme.css").read_text(encoding="utf-8")

    for selector in (
        ".queue-panel { position:relative; background:linear-gradient(180deg,#e7f5ef",
        ".ai-summary { margin:0 20px; border-color:#9fd9cb",
        ".reading-pane { background:linear-gradient(180deg,#fffefa,#fffdfa); }",
        ".action-rail { margin:18px 18px 18px 0; border:1px solid #e6c982",
    ):
        assert selector in stylesheet


def test_dark_content_decision_areas_do_not_leave_light_component_islands():
    stylesheet = Path("posts/static/posts/operations_theme.css").read_text(encoding="utf-8")

    for selector in (
        'body[data-operations-theme="dark"] #post-workspace .ai-summary { color:#edf8f4',
        'body[data-operations-theme="dark"] #post-workspace .ai-summary :is(.ai-summary-metrics>div,.risk-evidence,.ai-operation-followup)',
        'body[data-operations-theme="dark"] #post-workspace .action-rail { border-color:rgba(255,190,105,.3)',
        'body[data-operations-theme="dark"] #post-workspace .action-rail-inner { color:#f8ecd7',
    ):
        assert selector in stylesheet


def test_dark_operations_controls_and_post_detail_do_not_leave_light_surfaces():
    stylesheet = Path("posts/static/posts/operations_theme.css").read_text(encoding="utf-8")

    for selector in (
        'body[data-operations-theme="dark"] .filter-actions a { border-color:rgba(220,235,248,.18)',
        'body[data-operations-theme="dark"] .report-filters { border-color:rgba(220,235,248,.15)',
        'body[data-operations-theme="dark"] .metric-good { color:#cfe9df!important',
        'body[data-operations-theme="dark"] .mute-data-maintenance[open] summary { border-color:rgba(220,235,248,.12); background:#111923; }',
        'body[data-operations-theme="dark"] :is(.mute-batch-toolbar button,.mute-intent-button,.mute-history-link) { border-color:rgba(220,235,248,.18); color:#e6edf5; background:#111923; }',
        'body[data-operations-theme="dark"] .mute-confirm-dialog .mute-confirm-note { border-left-color:rgba(205,180,255,.7); color:#ded4f4; background:#201c30; }',
        '.detail-grid { display:grid; grid-template-columns:minmax(0,1fr) 264px',
        'body[data-operations-theme="dark"] :is(.post-card,.side-card) { border-color:rgba(220,235,248,.14)',
    ):
        assert selector in stylesheet


def test_theme_control_has_one_unambiguous_lucide_state_per_theme():
    stylesheet = Path("posts/static/posts/operations_theme.css").read_text(encoding="utf-8")

    assert "flex:0 0 36px" in stylesheet
    assert ".operations-theme-toggle span { position:absolute; top:50%; left:50%" in stylesheet
    assert ".operations-theme-toggle-sun { opacity:1; transform:translate(-50%,-50%)" in stylesheet
    assert ".operations-theme-toggle-moon { opacity:0" in stylesheet
    assert '.operations-theme-toggle[aria-pressed="true"] .operations-theme-toggle-moon { opacity:1; transform:translate(-50%,-50%)' in stylesheet


def test_open_filter_promotes_its_stacking_context_above_the_workspace():
    stylesheet = Path("posts/static/posts/operations.css").read_text(encoding="utf-8")

    assert ".workspace-filter:has(.multi-filter[open],.single-filter[open]) { z-index:3; }" in stylesheet


def test_dark_operation_dialog_overrides_id_scoped_light_surfaces():
    stylesheet = Path("posts/static/posts/operations_theme.css").read_text(encoding="utf-8")

    for selector in (
        'body[data-operations-theme="dark"] .operation-modal#operation-modal fieldset { border-color:rgba(220,235,248,.16); background:#111923; }',
        'body[data-operations-theme="dark"] .operation-modal#operation-modal .operation-permanent-mute { border-color:rgba(138,240,198,.25)',
        'body[data-operations-theme="dark"] .operation-modal#operation-modal .operation-footer { border-top-color:rgba(220,235,248,.16); background:rgba(16,21,29,.97); }',
    ):
        assert selector in stylesheet


def test_dark_topbar_matches_the_id_specificity_of_the_light_header_rule():
    stylesheet = Path("posts/static/posts/operations_theme.css").read_text(encoding="utf-8")

    assert 'body[data-operations-theme="dark"] #header.topbar,body[data-operations-theme="dark"] #header.topbar.admin-topbar' in stylesheet
    assert 'body[data-operations-theme="dark"] #header.topbar nav a { color:#9eacbc; }' in stylesheet


def test_dark_ai_review_correction_dialog_overrides_light_action_cards_as_one_component():
    stylesheet = Path("posts/static/posts/operations_theme.css").read_text(encoding="utf-8")

    for selector in (
        'body[data-operations-theme="dark"] .review-escalate-modal { border-top-color:#f5ba57',
        'body[data-operations-theme="dark"] .review-escalate-modal .review-correction-actions { border-color:rgba(245,186,87,.3)!important',
        'body[data-operations-theme="dark"] .review-escalate-modal .review-correction-actions label { border-color:rgba(220,235,248,.15)',
        'body[data-operations-theme="dark"] .review-escalate-modal .review-correction-actions label:has(input:checked) { border-color:#f5ba57',
        'body[data-operations-theme="dark"] .review-escalate-modal :is(.review-correction-reasons,.review-correction-duration) { border-color:rgba(220,235,248,.16)!important',
        'body[data-operations-theme="dark"] .review-escalate-modal #review-correction-permanent { border-color:rgba(138,240,198,.28)',
        'body[data-operations-theme="dark"] .review-escalate-modal .operation-footer .default { border-color:#f5ba57',
    ):
        assert selector in stylesheet
