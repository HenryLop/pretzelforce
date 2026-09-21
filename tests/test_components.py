"""Path -> component mapping, one row per shape the SFDX source format can take."""

from __future__ import annotations

import pytest

from pretzel.review.components import UNMAPPED, locate

PKG = ("force-app",)
D = "force-app/main/default"


@pytest.mark.parametrize(
    ("path", "mtype", "name"),
    [
        (f"{D}/classes/AccountService.cls", "ApexClass", "AccountService"),
        (f"{D}/classes/AccountService.cls-meta.xml", "ApexClass", "AccountService"),
        (f"{D}/triggers/AccountTrigger.trigger", "ApexTrigger", "AccountTrigger"),
        (f"{D}/flows/Lead_Router.flow-meta.xml", "Flow", "Lead_Router"),
        (f"{D}/lwc/accountCard/accountCard.js", "LightningComponentBundle", "accountCard"),
        (f"{D}/lwc/accountCard/__tests__/accountCard.test.js", "LightningComponentBundle", "accountCard"),
        (f"{D}/aura/OppPanel/OppPanelController.js", "AuraDefinitionBundle", "OppPanel"),
        (f"{D}/objects/Account/Account.object-meta.xml", "CustomObject", "Account"),
        (f"{D}/objects/Account/fields/Region__c.field-meta.xml", "CustomField", "Account.Region__c"),
        (f"{D}/objects/Opportunity/recordTypes/Partner.recordType-meta.xml", "RecordType", "Opportunity.Partner"),
        (
            f"{D}/objects/Opportunity/validationRules/Discount_Approved.validationRule-meta.xml",
            "ValidationRule",
            "Opportunity.Discount_Approved",
        ),
        (f"{D}/objects/Opportunity/listViews/All.listView-meta.xml", "ListView", "Opportunity.All"),
        (f"{D}/objects/Opportunity/webLinks/Open.webLink-meta.xml", "WebLink", "Opportunity.Open"),
        (
            f"{D}/objects/Opportunity/businessProcesses/Sales.businessProcess-meta.xml",
            "BusinessProcess",
            "Opportunity.Sales",
        ),
        # Names are used verbatim: spaces, dashes and URL-encoded characters included.
        (
            f"{D}/layouts/OpportunityLineItem-Opportunity Product Layout.layout-meta.xml",
            "Layout",
            "OpportunityLineItem-Opportunity Product Layout",
        ),
        (
            f"{D}/profiles/Force%2Ecom - App Subscription User.profile-meta.xml",
            "Profile",
            "Force%2Ecom - App Subscription User",
        ),
        (f"{D}/profiles/Técnico de Campo.profile-meta.xml", "Profile", "Técnico de Campo"),
        (
            f"{D}/customMetadata/Approval_Route.Finance_Review.md-meta.xml",
            "CustomMetadata",
            "Approval_Route.Finance_Review",
        ),
        (f"{D}/labels/CustomLabels.labels-meta.xml", "CustomLabels", "CustomLabels"),
        (f"{D}/staticresources/pdfjs.resource", "StaticResource", "pdfjs"),
        (f"{D}/staticresources/pdfjs.resource-meta.xml", "StaticResource", "pdfjs"),
        (f"{D}/staticresources/icons/img/logo.png", "StaticResource", "icons"),
        (f"{D}/reports/Sales_KPI/Bookings.report-meta.xml", "Report", "Sales_KPI/Bookings"),
        (f"{D}/dashboards/Sales_KPI.dashboardFolder-meta.xml", "Dashboard", "Sales_KPI"),
        (f"{D}/settings/Account.settings-meta.xml", "Settings", "Account"),
        (f"{D}/externalClientApps/PartnerPortal.eca-meta.xml", "ExternalClientApplication", "PartnerPortal"),
        (f"{D}/flexipages/Order_Console.flexipage-meta.xml", "FlexiPage", "Order_Console"),
        (
            f"{D}/flowDefinitions/Request_Discount_Approval.flowDefinition-meta.xml",
            "FlowDefinition",
            "Request_Discount_Approval",
        ),
        # Nested package layout: anything may sit above the metadata folder.
        ("force-app/sales/classes/Quote.cls", "ApexClass", "Quote"),
    ],
)
def test_locate(path: str, mtype: str, name: str) -> None:
    loc = locate(path, PKG)
    assert loc is not None
    assert (loc.key.type, loc.key.name) == (mtype, name)


@pytest.mark.parametrize(
    "path",
    [
        f"{D}/lwc/jsconfig.json",  # tooling file, not a bundle
        f"{D}/objects/Account/weird/Thing.xml",
        f"{D}/unknownFolder/Thing.thing-meta.xml",
        f"{D}/classes/README.md",
    ],
)
def test_unknown_paths_are_unmapped_not_guessed(path: str) -> None:
    loc = locate(path, PKG)
    assert loc is not None and loc.key.type == UNMAPPED


def test_paths_outside_package_dirs_are_ignored() -> None:
    assert locate("README.md", PKG) is None
    assert locate("scripts/apex/hello.apex", PKG) is None


def test_bundle_anchor_is_the_folder() -> None:
    loc = locate(f"{D}/lwc/accountCard/accountCard.html", PKG)
    assert loc.anchor == f"{D}/lwc/accountCard" and loc.bundle
