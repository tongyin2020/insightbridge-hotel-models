"""
依赖安全扫描工具

功能:
- pip-audit: Python依赖漏洞扫描
- npm audit: Node.js依赖漏洞扫描
- 生成安全报告

适用于: DirectorAI, MARE, InsightBridge
"""

import os
import json
import subprocess
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from enum import Enum

logger = logging.getLogger(__name__)


class Severity(str, Enum):
    """漏洞严重等级"""
    CRITICAL = "critical"
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    INFO = "info"


@dataclass
class Vulnerability:
    """漏洞信息"""
    package: str
    installed_version: str
    vulnerable_versions: str
    patched_versions: Optional[str]
    severity: Severity
    cve_id: Optional[str]
    description: str
    recommendation: str
    source: str  # "pip" or "npm"


@dataclass
class ScanResult:
    """扫描结果"""
    scan_time: str
    project_path: str
    source: str
    total_packages: int
    vulnerabilities: List[Vulnerability]
    critical_count: int
    high_count: int
    moderate_count: int
    low_count: int
    
    @property
    def has_critical(self) -> bool:
        return self.critical_count > 0
    
    @property
    def has_high(self) -> bool:
        return self.high_count > 0
    
    @property
    def total_vulnerabilities(self) -> int:
        return len(self.vulnerabilities)
    
    def to_dict(self) -> Dict:
        result = asdict(self)
        result["vulnerabilities"] = [asdict(v) for v in self.vulnerabilities]
        return result


class SecurityScanner:
    """安全扫描器"""
    
    def __init__(self, output_dir: str = "/tmp/security_reports"):
        """
        Args:
            output_dir: 报告输出目录
        """
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
    
    def scan_python(self, project_path: str) -> Optional[ScanResult]:
        """
        扫描Python依赖漏洞
        
        Args:
            project_path: 项目路径 (包含requirements.txt)
        
        Returns:
            ScanResult 或 None (如果扫描失败)
        """
        requirements_file = os.path.join(project_path, "requirements.txt")
        if not os.path.exists(requirements_file):
            logger.warning(f"未找到 requirements.txt: {project_path}")
            return None
        
        vulnerabilities = []
        total_packages = 0
        
        try:
            # 使用 pip-audit 扫描
            result = subprocess.run(
                ["pip-audit", "-r", requirements_file, "--format", "json"],
                capture_output=True,
                text=True,
                timeout=300,
            )
            
            if result.stdout:
                audit_data = json.loads(result.stdout)
                
                # 统计包数量
                if isinstance(audit_data, dict):
                    deps = audit_data.get("dependencies", [])
                    total_packages = len(deps)
                    
                    for dep in deps:
                        vulns = dep.get("vulns", [])
                        for vuln in vulns:
                            vulnerabilities.append(Vulnerability(
                                package=dep.get("name", "unknown"),
                                installed_version=dep.get("version", "unknown"),
                                vulnerable_versions=vuln.get("vulnerable_range", "unknown"),
                                patched_versions=vuln.get("fix_versions", [None])[0] if vuln.get("fix_versions") else None,
                                severity=self._map_severity(vuln.get("severity", "unknown")),
                                cve_id=vuln.get("id"),
                                description=vuln.get("description", ""),
                                recommendation=f"升级到 {vuln.get('fix_versions', ['最新版本'])[0] if vuln.get('fix_versions') else '最新版本'}",
                                source="pip",
                            ))
                
                elif isinstance(audit_data, list):
                    # 旧版格式
                    for item in audit_data:
                        vulnerabilities.append(Vulnerability(
                            package=item.get("name", "unknown"),
                            installed_version=item.get("version", "unknown"),
                            vulnerable_versions=item.get("id", "unknown"),
                            patched_versions=item.get("fix_versions", [None])[0] if item.get("fix_versions") else None,
                            severity=self._map_severity("moderate"),
                            cve_id=item.get("id"),
                            description=item.get("description", ""),
                            recommendation="升级到最新版本",
                            source="pip",
                        ))
        
        except subprocess.TimeoutExpired:
            logger.error("pip-audit 扫描超时")
            return None
        except FileNotFoundError:
            logger.error("pip-audit 未安装，请运行: pip install pip-audit")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"解析 pip-audit 输出失败: {e}")
            return None
        except Exception as e:
            logger.error(f"Python依赖扫描失败: {e}")
            return None
        
        # 统计各等级数量
        counts = self._count_severities(vulnerabilities)
        
        return ScanResult(
            scan_time=datetime.now(timezone.utc).isoformat(),
            project_path=project_path,
            source="pip-audit",
            total_packages=total_packages,
            vulnerabilities=vulnerabilities,
            **counts,
        )
    
    def scan_nodejs(self, project_path: str) -> Optional[ScanResult]:
        """
        扫描Node.js依赖漏洞
        
        Args:
            project_path: 项目路径 (包含package.json)
        
        Returns:
            ScanResult 或 None (如果扫描失败)
        """
        package_json = os.path.join(project_path, "package.json")
        if not os.path.exists(package_json):
            logger.warning(f"未找到 package.json: {project_path}")
            return None
        
        vulnerabilities = []
        total_packages = 0
        
        try:
            # 使用 npm audit 扫描
            result = subprocess.run(
                ["npm", "audit", "--json"],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=project_path,
            )
            
            if result.stdout:
                audit_data = json.loads(result.stdout)
                
                # npm audit 输出格式
                metadata = audit_data.get("metadata", {})
                total_packages = metadata.get("totalDependencies", 0)
                
                advisories = audit_data.get("vulnerabilities", {})
                for pkg_name, pkg_data in advisories.items():
                    vuln = pkg_data.get("via", [{}])
                    if isinstance(vuln, list) and vuln:
                        vuln = vuln[0] if isinstance(vuln[0], dict) else {"title": str(vuln[0])}
                    elif not isinstance(vuln, dict):
                        vuln = {"title": str(vuln)}
                    
                    vulnerabilities.append(Vulnerability(
                        package=pkg_name,
                        installed_version=pkg_data.get("version", "unknown"),
                        vulnerable_versions=pkg_data.get("range", "unknown"),
                        patched_versions=pkg_data.get("fixAvailable", {}).get("version") if isinstance(pkg_data.get("fixAvailable"), dict) else None,
                        severity=self._map_severity(pkg_data.get("severity", "unknown")),
                        cve_id=vuln.get("cve") if isinstance(vuln, dict) else None,
                        description=vuln.get("title", "") if isinstance(vuln, dict) else str(vuln),
                        recommendation=f"运行 npm audit fix 或手动升级",
                        source="npm",
                    ))
        
        except subprocess.TimeoutExpired:
            logger.error("npm audit 扫描超时")
            return None
        except FileNotFoundError:
            logger.error("npm 未安装")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"解析 npm audit 输出失败: {e}")
            return None
        except Exception as e:
            logger.error(f"Node.js依赖扫描失败: {e}")
            return None
        
        counts = self._count_severities(vulnerabilities)
        
        return ScanResult(
            scan_time=datetime.now(timezone.utc).isoformat(),
            project_path=project_path,
            source="npm-audit",
            total_packages=total_packages,
            vulnerabilities=vulnerabilities,
            **counts,
        )
    
    def scan_all(self, project_paths: List[Dict[str, str]]) -> Dict[str, List[ScanResult]]:
        """
        扫描多个项目
        
        Args:
            project_paths: 项目列表, 每项包含 {"name": "项目名", "path": "路径", "type": "python/nodejs/both"}
        
        Returns:
            {"project_name": [ScanResult, ...], ...}
        """
        results = {}
        
        for project in project_paths:
            name = project["name"]
            path = project["path"]
            proj_type = project.get("type", "both")
            
            project_results = []
            
            if proj_type in ("python", "both"):
                python_result = self.scan_python(path)
                if python_result:
                    project_results.append(python_result)
            
            if proj_type in ("nodejs", "both"):
                nodejs_result = self.scan_nodejs(path)
                if nodejs_result:
                    project_results.append(nodejs_result)
            
            results[name] = project_results
        
        return results
    
    def generate_report(
        self,
        results: Dict[str, List[ScanResult]],
        output_format: str = "json",
    ) -> str:
        """
        生成安全报告
        
        Args:
            results: scan_all 的返回结果
            output_format: "json" 或 "markdown"
        
        Returns:
            报告文件路径
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        
        if output_format == "json":
            filename = f"security_report_{timestamp}.json"
            filepath = os.path.join(self.output_dir, filename)
            
            report_data = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "projects": {
                    name: [r.to_dict() for r in scan_results]
                    for name, scan_results in results.items()
                },
                "summary": self._generate_summary(results),
            }
            
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(report_data, f, ensure_ascii=False, indent=2)
        
        else:  # markdown
            filename = f"security_report_{timestamp}.md"
            filepath = os.path.join(self.output_dir, filename)
            
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(self._generate_markdown_report(results))
        
        logger.info(f"安全报告已生成: {filepath}")
        return filepath
    
    def _map_severity(self, severity: str) -> Severity:
        """映射严重等级"""
        mapping = {
            "critical": Severity.CRITICAL,
            "high": Severity.HIGH,
            "moderate": Severity.MODERATE,
            "medium": Severity.MODERATE,
            "low": Severity.LOW,
            "info": Severity.INFO,
        }
        return mapping.get(severity.lower(), Severity.INFO)
    
    def _count_severities(self, vulnerabilities: List[Vulnerability]) -> Dict[str, int]:
        """统计各等级漏洞数量"""
        counts = {
            "critical_count": 0,
            "high_count": 0,
            "moderate_count": 0,
            "low_count": 0,
        }
        for vuln in vulnerabilities:
            if vuln.severity == Severity.CRITICAL:
                counts["critical_count"] += 1
            elif vuln.severity == Severity.HIGH:
                counts["high_count"] += 1
            elif vuln.severity == Severity.MODERATE:
                counts["moderate_count"] += 1
            elif vuln.severity == Severity.LOW:
                counts["low_count"] += 1
        return counts
    
    def _generate_summary(self, results: Dict[str, List[ScanResult]]) -> Dict:
        """生成汇总信息"""
        total_vulns = 0
        total_critical = 0
        total_high = 0
        projects_with_issues = []
        
        for name, scan_results in results.items():
            for result in scan_results:
                total_vulns += result.total_vulnerabilities
                total_critical += result.critical_count
                total_high += result.high_count
                
                if result.has_critical or result.has_high:
                    projects_with_issues.append(name)
        
        return {
            "total_vulnerabilities": total_vulns,
            "critical_vulnerabilities": total_critical,
            "high_vulnerabilities": total_high,
            "projects_with_critical_or_high": list(set(projects_with_issues)),
            "risk_level": "HIGH" if total_critical > 0 else ("MEDIUM" if total_high > 0 else "LOW"),
        }
    
    def _generate_markdown_report(self, results: Dict[str, List[ScanResult]]) -> str:
        """生成Markdown格式报告"""
        lines = [
            "# 依赖安全扫描报告",
            "",
            f"**生成时间:** {datetime.now(timezone.utc).isoformat()}",
            "",
            "---",
            "",
            "## 概要",
            "",
        ]
        
        summary = self._generate_summary(results)
        lines.extend([
            f"- **总漏洞数:** {summary['total_vulnerabilities']}",
            f"- **严重 (Critical):** {summary['critical_vulnerabilities']}",
            f"- **高危 (High):** {summary['high_vulnerabilities']}",
            f"- **风险等级:** {summary['risk_level']}",
            "",
        ])
        
        if summary['projects_with_critical_or_high']:
            lines.extend([
                "### ⚠️ 需要立即处理的项目:",
                "",
            ])
            for proj in summary['projects_with_critical_or_high']:
                lines.append(f"- {proj}")
            lines.append("")
        
        lines.extend([
            "---",
            "",
            "## 详细结果",
            "",
        ])
        
        for name, scan_results in results.items():
            lines.extend([
                f"### {name}",
                "",
            ])
            
            for result in scan_results:
                lines.extend([
                    f"**扫描工具:** {result.source}",
                    f"**扫描时间:** {result.scan_time}",
                    f"**包总数:** {result.total_packages}",
                    f"**漏洞数:** {result.total_vulnerabilities}",
                    "",
                ])
                
                if result.vulnerabilities:
                    lines.extend([
                        "| 包名 | 当前版本 | 严重等级 | CVE | 建议 |",
                        "|------|---------|---------|-----|------|",
                    ])
                    
                    for vuln in result.vulnerabilities:
                        lines.append(
                            f"| {vuln.package} | {vuln.installed_version} | "
                            f"{vuln.severity.value} | {vuln.cve_id or 'N/A'} | "
                            f"{vuln.recommendation} |"
                        )
                    
                    lines.append("")
                else:
                    lines.extend([
                        "✅ 未发现已知漏洞",
                        "",
                    ])
            
            lines.append("")
        
        lines.extend([
            "---",
            "",
            "## 修复建议",
            "",
            "1. **立即修复所有 Critical 和 High 级别漏洞**",
            "2. 运行 `pip-audit --fix` 自动修复Python依赖",
            "3. 运行 `npm audit fix` 自动修复Node.js依赖",
            "4. 定期运行安全扫描 (建议每周或每次部署前)",
            "5. 启用依赖版本锁定 (pip freeze, package-lock.json)",
            "",
        ])
        
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI 命令行工具
# ---------------------------------------------------------------------------

def main():
    """命令行入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description="依赖安全扫描工具")
    parser.add_argument("--project", "-p", required=True, help="项目路径")
    parser.add_argument("--type", "-t", choices=["python", "nodejs", "both"], default="both", help="项目类型")
    parser.add_argument("--output", "-o", default="/tmp/security_reports", help="报告输出目录")
    parser.add_argument("--format", "-f", choices=["json", "markdown"], default="markdown", help="报告格式")
    
    args = parser.parse_args()
    
    scanner = SecurityScanner(output_dir=args.output)
    
    results = scanner.scan_all([{
        "name": os.path.basename(args.project),
        "path": args.project,
        "type": args.type,
    }])
    
    report_path = scanner.generate_report(results, args.format)
    print(f"报告已生成: {report_path}")
    
    # 如果有严重漏洞，返回非零退出码
    summary = scanner._generate_summary(results)
    if summary["critical_vulnerabilities"] > 0:
        exit(2)
    elif summary["high_vulnerabilities"] > 0:
        exit(1)


if __name__ == "__main__":
    main()
