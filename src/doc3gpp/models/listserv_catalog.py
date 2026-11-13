"""Canonical reference entries for known ETSI LISTSERV mailing lists."""

from __future__ import annotations

from doc3gpp.models.listserv import normalize_list_id


def infer_listserv_tsg(list_id: str) -> str | None:
    """Infer a candidate TSG code from a normalized LISTSERV list ID."""
    normalized = normalize_list_id(list_id)
    prefix = "3GPP_TSG_"
    if not normalized.startswith(prefix):
        return None

    family_and_suffix = normalized[len(prefix) :]
    family, separator, suffix = family_and_suffix.partition("_")
    family_codes = {
        "RAN": ("R", "RP"),
        "SA": ("S", "SP"),
        "CT": ("C", "CP"),
    }
    codes = family_codes.get(family)
    if codes is None:
        return None

    code_prefix, plenary_code = codes
    if separator:
        wg_segment = suffix.partition("_")[0]
        if wg_segment.startswith("WG"):
            digits = wg_segment[2:]
            if digits and all("0" <= digit <= "9" for digit in digits):
                return f"{code_prefix}{digits}"
    return plenary_code


LISTSERV_CATALOG_SEEDS: tuple[tuple[str, str, str], ...] = (
    ("3GPP_TSG_CT", "CP", "3GPP_TSG_CT - Core Network and Terminals"),
    ("3GPP_TSG_CT_IETF-COORD", "CP", "3GPP_TSG_CT_IETF-Coord"),
    ("3GPP_TSG_CT_WG1", "C1", "3GPP_TSG_CT_WG1 - Core Network and Terminals WG 1"),
    (
        "3GPP_TSG_CT_WG1_122E_5G",
        "C1",
        "3GPP_TSG_CT_WG1_122e_5G Closed 15/06/2020 done by TGV by request #2020-102749",
    ),
    ("3GPP_TSG_CT_WG1_E_IMS", "C1", "3GPP_TSG_CT_WG1_E_IMS"),
    ("3GPP_TSG_CT_WG1_E_MAIN", "C1", "3GPP_TSG_CT_WG1_E_main"),
    ("3GPP_TSG_CT_WG1_E_MC", "C1", "3GPP_TSG_CT_WG1_E_MC"),
    ("3GPP_TSG_CT_WG1_E_SERVICES", "C1", "3GPP_TSG_CT_WG1_E_services"),
    ("3GPP_TSG_CT_WG3", "C3", "3GPP_TSG_CT_WG3 - Core Network and Terminals WG 3"),
    (
        "3GPP_TSG_CT_WG3_108E_MAIN",
        "C3",
        "3GPP_TSG_CT_WG3_108e_main Closed 02/12/2020, #done by JMS by request Anna ticket n°2020-105066",
    ),
    (
        "3GPP_TSG_CT_WG3_108E_NON_PCC",
        "C3",
        "3GPP_TSG_CT_WG3_108e_Non_PCC Closed 02/12/2020, #done by JMS by request Anna ticket n°2020-105066",
    ),
    (
        "3GPP_TSG_CT_WG3_108E_PCC",
        "C3",
        "3GPP_TSG_CT_WG3_108e_PCC Closed 02/12/2020, #done by JMS by request Anna ticket n°2020-105066",
    ),
    ("3GPP_TSG_CT_WG3_MAIN", "C3", "3GPP_TSG_CT_WG3_main"),
    ("3GPP_TSG_CT_WG3_NON_PCC", "C3", "3GPP_TSG_CT_WG3_Non_PCC"),
    ("3GPP_TSG_CT_WG3_PCC", "C3", "3GPP_TSG_CT_WG3_PCC"),
    ("3GPP_TSG_CT_WG4", "C4", "3GPP_TSG_CT_WG4 - Core Network and Terminals WG 4"),
    (
        "3GPP_TSG_CT_WG4_E_MEETING",
        "C4",
        "3GPP_TSG_CT_WG4_E_Meeting - Core Network and Terminals WG 4 E_Meeting",
    ),
    ("3GPP_TSG_CT_WG6", "C6", "3GPP_TSG_CT_WG6 - Core Network and Terminals WG 6"),
    ("3GPP_TSG_RAN", "RP", "3gpp_tsg_ran: tsg radio access network group"),
    ("3GPP_TSG_RAN_ADHOC", "R2", "3gpp_tsg_ran_adhoc: tsg ran ad hoc meetings"),
    (
        "3GPP_TSG_RAN_AHG1",
        "RT",
        "3gpp_tsg_ran_ahg1: tsg ran ad-hoc group on itu co-ordination",
    ),
    ("3GPP_TSG_RAN_DRAFTS", "RP", "3GPP_TSG_RAN_DRAFTS"),
    ("3GPP_TSG_RAN_WG1", "R1", "3gpp_tsg_ran_wg1: tsg ran working group 1"),
    ("3GPP_TSG_RAN_WG1_CHANNELMODEL", "R1", "3GPP_TSG_RAN_WG1_ChannelModel"),
    ("3GPP_TSG_RAN_WG1_EUL_AH", "R1", "3GPP_TSG_RAN_WG1_EUL_AH"),
    ("3GPP_TSG_RAN_WG1_HSPA", "R1", "3GPP_TSG_RAN_WG1_HSPA"),
    ("3GPP_TSG_RAN_WG1_LTE", "R1", "3GPP_TSG_RAN_WG1_LTE"),
    ("3GPP_TSG_RAN_WG1_NR", "R1", "3GPP_TSG_RAN_WG1_NR"),
    ("3GPP_TSG_RAN_WG1_WCDMA_MIMO", "R1", "3GPP_TSG_RAN_WG1_WCDMA_MIMO"),
    ("3GPP_TSG_RAN_WG2", "R2", "3GPP_TSG_RAN_WG2 list"),
    (
        "3GPP_TSG_RAN_WG2_MBMS_RRC",
        "R2",
        "3GPP_TSG_RAN_WG2_MBMS_RRC List list closed 26th August 2009",
    ),
    (
        "3GPP_TSG_RAN_WG2_ROHC",
        "R2",
        "3GPP_TSG_RAN_WG2_ROHC : 3GPP_TSG_RAN_WG2_ROHC List Closed 26th August 2009",
    ),
    (
        "3GPP_TSG_RAN_WG2_UMTS_DRAFTS",
        "R2",
        "Closed 05/03/2025 Done by PKA by request of Joern K ticket 2025-101007",
    ),
    ("3GPP_TSG_RAN_WG3", "R3", "3gpp_tsg_ran_wg3: tsg ran working group 3"),
    ("3GPP_TSG_RAN_WG4", "R4", "3gpp_tsg_ran_wg4: tsg ran working group 4"),
    ("3GPP_TSG_RAN_WG4_AAS", "R4", "3GPP_TSG_RAN_WG4_AAS"),
    (
        "3GPP_TSG_RAN_WG4_BANDS",
        "R4",
        "3GPP_TSG_RAN_WG4_Bands : New UMTS frequency bands list closed",
    ),
    ("3GPP_TSG_RAN_WG4_CA", "R4", "3GPP_TSG_RAN_WG4_CA"),
    (
        "3GPP_TSG_RAN_WG4_DIVERSITY_OFF",
        "R4",
        "3gpp_tsg_ran_wg4_DIVERSITY_OFF: tsg ran working group 4 Diversity off list closed",
    ),
    ("3GPP_TSG_RAN_WG4_DRAFTS", "R4", "3GPP_TSG_RAN_WG4_DRAFTS"),
    ("3GPP_TSG_RAN_WG4_EMEETING", "R4", "3GPP_TSG_RAN_WG4_eMeeting"),
    (
        "3GPP_TSG_RAN_WG4_HSDPA",
        "R4",
        "3GPP_TSG_RAN_WG4_HSDPA : HSDPA PERFORMANCE REQUIREMENTS",
    ),
    ("3GPP_TSG_RAN_WG4_IOT", "R4", "3GPP_TSG_RAN_WG4_IoT"),
    ("3GPP_TSG_RAN_WG4_LTE", "R4", "3GPP TSG RAN WG4 Long Term Evolution"),
    ("3GPP_TSG_RAN_WG4_MIMO-OTA", "R4", "3GPP_TSG_RAN_WG4_MIMO-OTA"),
    ("3GPP_TSG_RAN_WG4_NR_BANDS", "R4", "3GPP_TSG_RAN_WG4_NR_band_and_combo"),
    ("3GPP_TSG_RAN_WG4_NR-MIMO-OTA", "R4", "3GPP_TSG_RAN_WG4_NR-MIMO-OTA"),
    ("3GPP_TSG_RAN_WG4_TRP-TRS-OTA", "R4", "3GPP_TSG_RAN_WG4_TRP-TRS-OTA"),
    ("3GPP_TSG_RAN_WG5", "R5", "3GPP_TSG_RAN_WG5 : Testing"),
    ("3GPP_TSG_RAN_WG5_5G_NR", "R5", "3GPP_TSG_RAN_WG5_5G_NR"),
    ("3GPP_TSG_RAN_WG5_6G_SPEC_MODERN", "R5", "3GPP_TSG_RAN_WG5_6G_Spec_Modern"),
    ("3GPP_TSG_RAN_WG5_AIML", "R5", "3GPP_TSG_RAN_WG5_AIML"),
    ("3GPP_TSG_RAN_WG5_ANALYTICS", "R5", "3GPP_TSG_RAN_WG5_ANALYTICS"),
    ("3GPP_TSG_RAN_WG5_APPLICABILITY", "R5", "3GPP_TSG_RAN_WG5_APPLICABILITY"),
    ("3GPP_TSG_RAN_WG5_FR2_MU", "R5", "3GPP_TSG_RAN_WG5_FR2_MU"),
    (
        "3GPP_TSG_RAN_WG5_IMSCC",
        "R5",
        "3GPP TSG RAN WG5 IMS Call Control mailing",
    ),
    ("3GPP_TSG_RAN_WG5_IOT", "R5", "3GPP_TSG_RAN_WG5_IOT"),
    ("3GPP_TSG_RAN_WG5_LTE-CDMA2000", "R5", "3GPP_TSG_RAN_WG5_LTE-CDMA2000"),
    ("3GPP_TSG_RAN_WG5_MCX", "R5", "3GPP_TSG_RAN_WG5_MCX"),
    ("3GPP_TSG_RAN_WG5_RF", "R5", "3GPP_TSG_RAN_WG5_RF : Radio Frequency"),
    ("3GPP_TSG_RAN_WG5_SIG", "R5", "3GPP_TSG_RAN_WG5_SIG: Signalling"),
    ("3GPP_TSG_RAN_WG5_TRP-TRS-OTA", "R5", "3GPP_TSG_RAN_WG5_TRP-TRS-OTA"),
    (
        "3GPP_TSG_RAN_WG6",
        "R6",
        "3GPP_TSG_RAN_WG6 Close 14/08/2020 JMS ticket 2020-103474",
    ),
    ("3GPP_TSG_SA", "SP", "3gpp_tsg_sa: tsg system aspects group"),
    ("3GPP_TSG_SA_DRAFTS", "S2", "3GPP_TSG_SA_DRAFTS"),
    (
        "3GPP_TSG_SA_ITUT_AHG",
        "SP",
        "3gpp_tsg_sa_ITUT_AHG: TSG SA ad-hoc group on ITU-T co-ordination",
    ),
    ("3GPP_TSG_SA_WG1", "S1", "3gpp_tsg_sa_wg1: tsg sa services"),
    ("3GPP_TSG_SA_WG2", "S2", "3gpp_tsg_sa_wg2: tsg sa architecture"),
    ("3GPP_TSG_SA_WG2_DISCUSSIONS", "S2", "3GPP_TSG_SA_WG2_DISCUSSIONS"),
    ("3GPP_TSG_SA_WG2_ELCS", "S2", "3GPP_TSG_SA_WG2_eLCS"),
    ("3GPP_TSG_SA_WG2_EMEET", "S2", "3GPP_TSG_SA_WG2_EMEET"),
    ("3GPP_TSG_SA_WG2_MTCE", "S2", "3GPP_TSG_SA_WG2_MTCe"),
    (
        "3GPP_TSG_SA_WG2_PROSE_GCSE_LTE",
        "S2",
        "3GPP_TSG_SA_WG2_ProSe_GCSE_LTE",
    ),
    ("3GPP_TSG_SA_WG2_UPCON", "S2", "3GPP_TSG_SA_WG2_UPCON"),
    ("3GPP_TSG_SA_WG3", "S3", "3gpp_tsg_sa_wg3: tsg sa security"),
    (
        "3GPP_TSG_SA_WG3_LI",
        "S3",
        "3gpp_tsg_sa_wg3_li: (tsg sa wg3 lawful interception group)",
    ),
    ("3GPP_TSG_SA_WG4", "S4", "3gpp_tsg_sa_wg4: tsg sa codec"),
    ("3GPP_TSG_SA_WG4_AUDIO", "S4", "3GPP_TSG_SA_WG4_AUDIO"),
    ("3GPP_TSG_SA_WG4_EVS-IVAS", "S4", "3GPP_TSG_SA_WG4_EVS-IVAS"),
    ("3GPP_TSG_SA_WG4_MBS", "S4", "3GPP_TSG_SA_WG4_MBS"),
    (
        "3GPP_TSG_SA_WG4_MTSI",
        "S4",
        "3GPP_TSG_SA_WG4_MTSI Closed 31/03/2-#done by YLC-By Request of Jayeeta Saha-ticket n°2022-101512",
    ),
    ("3GPP_TSG_SA_WG4_RTC", "S4", "3GPP_TSG_SA_WG4_RTC"),
    (
        "3GPP_TSG_SA_WG4_SQ",
        "S4",
        "3GPP_TSG_SA_WG4_SQ-Closed 31/03/22-#done by YLC-by request of Jayeeta Saha-ticket n°2022-101512",
    ),
    ("3GPP_TSG_SA_WG4_VIDEO", "S4", "3GPP_TSG_SA_WG4_VIDEO"),
    ("3GPP_TSG_SA_WG5", "S5", "3gpp_tsg_sa_wg 5: tsg sa5 telecoms management"),
    (
        "3GPP_TSG_SA_WG5_CHARGING",
        "S5",
        "3GPP_TSG_SA_WG5_Charging: Charging Management",
    ),
    ("3GPP_TSG_SA_WG5_OAM", "S5", "3GPP_TSG_SA_WG5_OAM : OAM"),
    (
        "3GPP_TSG_SA_WG5_SWGA",
        "S5",
        "3GPP_TSG_SA_WG5_SWGA : Closed 13/12/2019 done by Guillaume T by request Ticket n°2019-103870",
    ),
    ("3GPP_TSG_SA_WG5_SWGB", "S5", "3GPP_TSG_SA_WG5_SWGB : Charging CLOSED 270407"),
    (
        "3GPP_TSG_SA_WG5_SWGC",
        "S5",
        "3GPP_TSG_SA_WG5_SWGC : OAM&P Interface Definition Closed 270407",
    ),
    (
        "3GPP_TSG_SA_WG5_SWGD",
        "S5",
        "3GPP_TSG_SA_WG5_SWGD : OAM&P Data Definition CLOSED 270407",
    ),
    ("3GPP_TSG_SA_WG6", "S6", "3gpp_tsg_sa_wg6: mission-critical applications"),
    ("3GPP_TSG_SA1_EMEET", "S1", "3GPP_TSG_SA1_eMeet"),
    ("3GPP_TSG_SA4_FS_6G_MED", "S4", "3GPP_TSG_SA4_FS_6G_MED"),
    (
        "3GPP_TSG_SA4_FS_MED",
        "S4",
        "Closed 21/09/2026 #done by PKA by request of Andrijana Brekalo ticket n°2026-104353",
    ),
    ("3GPP_TSG_SA4_IVAS_BASOP", "S4", "3GPP_TSG_SA4_IVAS_BASOP"),
    (
        "3GPP_TSG_SA4_IVAS_PUBLIC_CLBRTN",
        "S4",
        "3gpp_tsg_sa_wg4_IVAS_Public_Collaboration@list.etsi.org",
    ),
)
